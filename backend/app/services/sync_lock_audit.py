"""Bounded static audit of synchronous locking reachable from async bodies.

Recognizes method/keyword row locks, literal blocking PostgreSQL advisory SQL,
imports/local helper chains and explicit on_event callbacks. Worker operation
arguments are cut at run_phase/to_thread/run_in_threadpool boundaries. It is
not a Python dynamic call-graph proof: reflection, monkeypatches, arbitrary
object method dispatch, variable-built SQL and ORM events need runtime review.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

WORKERS = {'run_phase', 'to_thread', 'run_in_threadpool'}


def body_nodes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from body_nodes(child)


def audit_sources(sources: dict[str, str]):
    functions = {}; imports = {}; modules = {}; inventory = []; cuts = []
    for path, source in sources.items():
        module = path.removesuffix('.py').replace('/', '.')
        if module.endswith('.__init__'):module = module[:-9]
        tree = ast.parse(source); modules[path] = module
        aliases = imports.setdefault(module, {})
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                prefix = node.module or ''
                if node.level:
                    base = module.split('.')[:-node.level]
                    prefix = '.'.join([*base, prefix]).rstrip('.')
                for item in node.names:
                    aliases.setdefault(item.asname or item.name, set()).add(prefix + '.' + item.name)
            elif isinstance(node, ast.Import):
                for item in node.names:
                    aliases.setdefault(item.asname or item.name.split('.')[0], set()).add(
                        item.name if item.asname else item.name.split('.')[0])
        def collect(nodes, scope=''):
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f'{scope}.{node.name}'.strip('.')
                    functions[module+'.'+name] = (path,node,module,name)
                    collect(node.body,name)
                elif isinstance(node,ast.ClassDef):collect(node.body,f'{scope}.{node.name}'.strip('.'))
        collect(tree.body)

    def resolve(expression, module, scope):
        parts = expression.split('.'); result = set()
        # Closest lexical definition, then module imports. No leaf-name guessing.
        for n in range(len(scope.split('.')),-1,-1):
            stem = '.'.join([module,*scope.split('.')[:n],expression])
            if stem in functions:result.add(stem);break
        for prefix in imports.get(module,{}).get(parts[0],()):
            target='.'.join([prefix,*parts[1:]])
            if target in functions:result.add(target)
        if expression in functions:result.add(expression)
        return result

    # Adapter arguments are cut only when the resolved function actually
    # forwards through the reviewed worker primitive. Its own body is still
    # audited, so adding a lock before run_phase is never hidden.
    adapters = set()
    for fid, (_, node, _, _) in functions.items():
        if node.name in {'phase', '_audited_response'} and any(
                isinstance(n, ast.Call) and ast.unparse(n.func).split('.')[-1]=='run_phase'
                for n in body_nodes(node)):
            adapters.add(fid)

    def conditional_reader(fid, call):
        _, node, _, _ = functions[fid]
        defaults = dict(zip([a.arg for a in node.args.kwonlyargs], node.args.kw_defaults))
        if not (isinstance(defaults.get('lock'), ast.Constant) and defaults['lock'].value is False):
            return False
        if any(k.arg=='lock' and not (isinstance(k.value,ast.Constant) and k.value.value is False) for k in call.keywords):
            return False
        guarded=set()
        for block in body_nodes(node):
            if isinstance(block,ast.If) and isinstance(block.test,ast.Name) and block.test.id=='lock':
                for statement in block.body:
                    guarded.update(id(n) for n in ast.walk(statement))
            if isinstance(block,ast.IfExp) and isinstance(block.test,ast.Name) and block.test.id=='lock':
                guarded.update(id(n) for n in ast.walk(block.body))
        locks=[n for n in body_nodes(node) if isinstance(n,ast.Call) and ast.unparse(n.func).endswith('.with_for_update')]
        return bool(locks) and all(id(n) in guarded for n in locks)

    edges = {}; primitive = {}; conditional_reads=[]
    for fid,(path,node,module,scope) in functions.items():
        edges[fid] = set(); locks = []
        def walk(current):
            for child in ast.iter_child_nodes(current):
                if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):continue
                if isinstance(child,ast.Call):
                    expression=ast.unparse(child.func); leaf=expression.split('.')[-1]
                    targets=resolve(expression,module,scope)
                    if leaf in WORKERS or (targets and targets.issubset(adapters)):
                        cuts.append({'function':fid,'line':child.lineno,'worker':expression})
                        continue  # worker callable/lambda executes off the caller loop
                    row = leaf=='with_for_update' or any(k.arg=='with_for_update' and
                        not (isinstance(k.value,ast.Constant) and k.value.value is False) for k in child.keywords)
                    sql = any(isinstance(a,ast.Constant) and isinstance(a.value,str)
                        and ('pg_advisory_xact_lock' in a.value.lower() or 'pg_advisory_lock(' in a.value.lower()
                             or 'for update' in a.value.lower()) for a in child.args)
                    if row or sql:locks.append({'line':child.lineno,'kind':'row' if row else 'advisory_sql','call':expression})
                    for target in targets:
                        if conditional_reader(target,child):
                            conditional_reads.append({'caller':fid,'callee':target,'line':child.lineno,'lock':False})
                        else:edges[fid].add(target)
                    for k in child.keywords:
                        if k.arg=='on_event' and isinstance(k.value,(ast.Name,ast.Attribute)):
                            edges[fid].update(resolve(ast.unparse(k.value),module,scope))
                yield child;yield from walk(child)
        list(walk(node))
        if locks:
            primitive[fid]=locks
            inventory.append({'function':fid,'path':path,'line':node.lineno,'locks':locks,
                              'source_sha256':hashlib.sha256(ast.get_source_segment(sources[path],node).encode()).hexdigest()})
    paths=[]
    for root,(_,node,_,_) in functions.items():
        if not isinstance(node,ast.AsyncFunctionDef):continue
        def visit(fid,trail):
            if fid in primitive:paths.append({'async_function':root,'chain':trail,'locks':primitive[fid]})
            for target in sorted(edges[fid]):
                # Async helpers are still traversed; worker barriers inside them cut safely.
                if target not in trail:visit(target,trail+[target])
        visit(root,[root])
    return {'scope':__doc__,'lock_functions':sorted(inventory,key=lambda x:x['function']),
            'async_lock_paths':paths,'worker_boundaries':cuts,'conditional_read_edges':conditional_reads}


def audit_tree(app_root: Path):
    # Key paths begin with app, matching absolute app imports.
    return audit_sources({str(p.relative_to(app_root.parent)):p.read_text()
                          for p in app_root.rglob('*.py')})
