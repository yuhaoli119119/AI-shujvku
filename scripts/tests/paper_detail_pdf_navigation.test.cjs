const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('frontend/pages/paper_detail/index.html','utf8');
const extract=name=>{const start=html.indexOf('    function '+name+'('),rest=html.slice(start),next=rest.slice(1).search(/\n    (?:async )?function /);return rest.slice(0,next+1)};
const sha='a'.repeat(64),other='b'.repeat(64),file={id:'f',paper_id:'p',role:'main',sha256:sha,original_filename:'source.pdf'};
const ctx=vm.createContext({currentSourceFile:id=>id==='f'?file:null,currentDisplayRole:f=>f.role,storedText:String,esc:String});
ctx.window=ctx;vm.runInContext(fs.readFileSync('frontend/shared/editable-data-grid.js','utf8'),ctx);
for(const f of ['readingLocatorIdentity','currentPdfLinks'])vm.runInContext(extract(f),ctx);
const links=i=>ctx.currentPdfLinks('f',[13],i);
test('legacy native PDF binds the registered bytes for old or missing metadata',()=>{for(const i of [null,{},{page_basis:'source_pdf'},{page_basis:'source_pdf',source_sha256:sha},{file_sha256:sha}]){const h=links(i);assert.match(h,/#page=13/);assert.match(h,/page_basis=source_pdf/);assert.match(h,new RegExp('reading_pdf_sha256='+sha));}});
test('wrong, partial derived, stale and cross-file identities remain blocked',()=>{for(const i of [{source_sha256:other},{page_basis:'derived_pdf',source_sha256:sha},{source_sha256:sha,reading_pdf_sha256:other,page_basis:'source_pdf'},{source_sha256:sha,page_identity_status:'stale'},{source_sha256:sha,file_id:'other'},{source_sha256:sha,paper_id:'foreign'},{source_sha256:sha,file_sha256:other}])assert.doesNotMatch(links(i),/#page=/);});
test('complete derived identity retains exact reading version',()=>{file.original_filename='source.docx';assert.match(links({page_basis:'derived_pdf',source_sha256:sha,reading_pdf_sha256:other}),new RegExp('reading_pdf_sha256='+other));file.original_filename='source.pdf';});
test('reading hash inheritance is same-file only and explicit mismatch survives',()=>{const reading={file_id:'f',file_sha256:sha};assert.match(links(ctx.readingLocatorIdentity({file_id:'f'},reading)),/#page=13/);assert.equal(ctx.readingLocatorIdentity({file_id:'g'},reading).file_sha256,undefined);assert.doesNotMatch(links(ctx.readingLocatorIdentity({file_id:'f',file_sha256:other},reading)),/#page=/);});

test('DOCX cannot infer reading identity from its source hash',()=>{file.original_filename='source.docx';assert.doesNotMatch(links({file_sha256:sha}),/#page=/);file.original_filename='source.pdf';});

test('legacy native source ID preserves stored unverified state without blocking navigation',()=>{const s={file_id:'f',paper_id:'p',page_identity_status:'legacy_unverified'};assert.match(links(s),/#page=13/);assert.doesNotMatch(links(s),/旧来源页码身份未核实/);assert.equal(s.source_sha256,undefined);file.original_filename='source.docx';assert.doesNotMatch(links(s),/#page=/);file.original_filename='source.pdf';});
