const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {spawn} = require('node:child_process');
const path = require('node:path');
const {WS_URI, safeIpcEndpoint, connectionDiagnostic} = require('../codex_web_dispatch.cjs');
test('host IPC default is 8214 and diagnostics never include URI credentials/query',()=>{
  if (!process.env.CODEX_WEB_IPC_URI) assert.equal(WS_URI,'ws://127.0.0.1:8214/__backend/ipc');
  const input='ws://user:private-password@127.0.0.1:49000/__backend/ipc?token=private-token';
  const output=connectionDiagnostic(input,new Error('Unexpected server response: 401'));
  assert(!output.includes('private-'));assert(!safeIpcEndpoint(input).includes('user'));
  assert(output.includes('认证代理'));assert(output.includes('未重试'));assert(output.includes('8214'));
});
test('env override reaches only specified local fixture, 401 fails once with actionable diagnosis',async()=>{
  let requests=0;
  const server=http.createServer((req,res)=>{requests++;res.writeHead(401);res.end('fixture authentication boundary');});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const code=path.resolve(__dirname,'../codex_web_dispatch.cjs');
    const child=spawn(process.execPath,[code,'models'],{env:{...process.env,CODEX_WEB_IPC_URI:`ws://127.0.0.1:${server.address().port}/__backend/ipc`},stdio:['ignore','pipe','pipe']});
    let stderr='';child.stderr.on('data',chunk=>stderr+=chunk);
    const exit=await new Promise(resolve=>child.once('exit',resolve));
    assert.equal(exit,1);assert.equal(requests,1);assert(stderr.includes('认证代理'));assert(stderr.includes('未重试'));
  } finally {await new Promise(resolve=>server.close(resolve));}
});
