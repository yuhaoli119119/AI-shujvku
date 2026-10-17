const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../frontend/pages/paper_detail/index.html'), 'utf8');
function source(name) {
  const start = html.search(new RegExp(`    (?:async )?function ${name}\\(`));
  assert(start >= 0, name);
  const tail = html.slice(start);
  const next = tail.slice(1).search(/\n    (?:async )?function /);
  return next < 0 ? tail : tail.slice(0, next + 1);
}
function setup() {
  const ctx = vm.createContext({
    state: {paperId:'paper'}, API_BASE:'/api/papers',
    esc: value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    currentPdfLinks: (file,pages)=>`<a href="/api/rebuild/files/${file}/preview#page=${pages[0]}">Pages ${pages.join(',')}</a>`,
    currentScientificReading:()=>null, renderFigureReadingHtml:()=>'',
  });
  for (const name of ['visualLabelKey','renderPipeTable','tableBodyContent','tableClipboardText','copyTableOriginal','renderTableBody','tableEntryFromItem','tableEntryFromRecord','groupTableEntries','renderTableGroup']) vm.runInContext(source(name),ctx);
  return ctx;
}
test('legacy markdown, modern markdown and structured strings take priority over context',()=>{
  const c=setup();
  assert.equal(c.tableBodyContent(null,{markdown_content:'legacy'}).text,'legacy');
  assert.equal(c.tableBodyContent({provenance:{table_markdown:'new'},context_text:'fallback'},{markdown_content:'old'}).text,'new');
  const data={columns:['t','value'],rows:[['s','1.00s'],['unknown',null],['','0.0100']],footnotes:['missing is unreported']};
  const content=c.tableBodyContent({provenance:{table_data:data,table_markdown:'md'},context_text:'raw'},null);
  assert.equal(content.format,'structured');
  assert.equal(c.tableClipboardText(content),'t\tvalue\ns\t1.00s\nunknown\t\n\t0.0100\n\nmissing is unreported');
  assert(c.renderTableBody(content).includes('data-missing="true"'));
});
test('context-only raw text preserves whitespace, XSS remains inert, clipboard is exact',async()=>{
  const c=setup(), text='\n  s 1.00s\r\n<svg onload="alert(1)"> & \'\n| not a guessed table |';
  const content=c.tableBodyContent({context_text:text},null), rendered=c.renderTableBody(content);
  assert.equal(content.format,'text');assert(!rendered.includes('<svg'));assert(!rendered.includes('<table>'));
  const encoded=rendered.match(/data-table-copy="([^"]+)"/)[1].replace(/&#39;/g,"'");
  let copied;c.navigator={clipboard:{writeText:async value=>copied=value}};
  await c.copyTableOriginal({currentTarget:{getAttribute:()=>encoded}});
  assert.equal(copied,text);
});
test('S13 caption on 71 and body on 72 retain pages, pointers, repeated rows and each raw segment',()=>{
  const c=setup();
  const asset={id:'a',paper_id:'paper',asset_type:'table',file_id:'si',figure_label:'Table S13',caption:'caption page 71',page_numbers:[71,72],context_text:'s 1.00s\ns 1.00s',
    provenance:{table_sources:[{role:'caption',file_id:'si',page_number:71,locator:'bottom'},{role:'body',file_id:'si',page_number:72,locator:'top'}]}};
  const entry=c.tableEntryFromItem({figure:{_currentAsset:asset},text:null});
  const rendered=c.renderTableGroup({key:'s13',records:[entry]});
  assert(rendered.includes('71,72')); assert(rendered.includes('caption · 第 71 页'));assert(rendered.includes('body · 第 72 页'));
  assert.equal((rendered.match(/s 1.00s/g)||[]).length,2);
  const other={...asset,id:'b',page_numbers:[73],context_text:'0.0100\nmissing —'};
  const groups=c.groupTableEntries({tables:[{figure:{_currentAsset:asset}},{figure:{_currentAsset:other}}],remainingTables:[]});
  assert.equal(groups.length,1);const grouped=c.renderTableGroup(groups[0]);assert(grouped.includes('原始分段'));assert(grouped.includes('0.0100'));
  other.file_id='another-edition';assert.equal(c.groupTableEntries({tables:[{figure:{_currentAsset:asset}},{figure:{_currentAsset:other}}],remainingTables:[]}).length,2);
});
test('empty and malformed structured data remain honest; raw fallback does not invent cells',()=>{
  const c=setup();assert.equal(c.tableBodyContent({},null).format,'empty');
  assert(c.renderTableBody({format:'empty'}).includes('尚未保存'));
  assert.equal(c.tableBodyContent({provenance:{table_data:{columns:['a'],rows:[[1.00]]}},context_text:'1.00s'},null).format,'text');
  const md='| Type | Time |\n| --- | --- |\n| s | 1.00s |\n| missing | |\n\nOriginal footnote.';
  const rendered=c.renderTableBody({format:'markdown',text:md});assert(rendered.includes('1.00s'));assert(rendered.includes('Original footnote.'));assert.equal(c.tableClipboardText({format:'markdown',text:md}),md);
});
test('rebuilt tables sort main before SI and use numeric table labels',()=>{
  const c=setup();
  const tables=['S10','2','S2','10','S1','1'].map(label=>({figure:{_currentAsset:{
    id:label,paper_id:'paper',file_id:label.startsWith('S')?'si':'main',
    figure_label:'Table '+label,page_numbers:[1],context_text:'body '+label,
  }}}));
  const groups=c.groupTableEntries({tables,remainingTables:[]});
  assert.deepEqual(Array.from(groups,group=>group.records[0].label),['1','2','10','s1','s2','s10']);
});
test('legacy main and linked SI tables also use numeric label order',()=>{
  const c=setup();
  const remainingTables=['S10','2','S2','10','S1','1'].map(label=>({
    paper_id:label.startsWith('S')?'si-paper':'paper',
    related_paper_id:label.startsWith('S')?'si-paper':null,
    related_paper_code:label.startsWith('S')?'A077SI':null,
    caption:'Table '+label,page:1,markdown_content:'body '+label,
  }));
  const groups=c.groupTableEntries({tables:[],remainingTables});
  assert.deepEqual(Array.from(groups,group=>group.records[0].label),['1','2','10','s1','s2','s10']);
});
test('numeric group sorting retains cross-page order and separates source files',()=>{
  const c=setup();
  const item=(label,file,page)=>({figure:{_currentAsset:{id:file+'-'+page,
    paper_id:'paper',file_id:file,figure_label:label,page_numbers:[page],context_text:'page '+page}}});
  const groups=c.groupTableEntries({tables:[item('S13','si',72),item('S2','si',20),
    item('S13','si',71),item('S13','another-edition',80),item('2','main',2)],remainingTables:[]});
  assert.deepEqual(Array.from(groups,group=>group.records[0].label),['2','s2','s13','s13']);
  const crossPage=groups.find(group=>group.records[0].asset.file_id==='si'&&group.records[0].label==='s13');
  assert.deepEqual(Array.from(crossPage.records,entry=>entry.page),[71,72]);
  assert.equal(crossPage.records.length,2);
  const otherEdition=groups.find(group=>group.records[0].asset.file_id==='another-edition');
  assert.equal(otherEdition.records.length,1);assert.notEqual(otherEdition.key,crossPage.key);
});
test('A077 metadata projection orders all 18 bare stored SI labels numerically',()=>{
  const c=setup();
  // Read-only projection of the observed asset-key order, bare labels and PDF pages.
  // Scientific table bodies and production identifiers are deliberately unnecessary.
  const metadata=[['S1',[67]],['S10',[70]],['S11',[71]],['S12',[71]],['S13',[71,72]],
    ['S14',[72]],['S15',[73]],['S16',[73]],['S17',[74]],['S18',[74]],['S2',[67]],
    ['S3',[67]],['S4',[68]],['S5',[68]],['S6',[68]],['S7',[69]],['S8',[69]],['S9',[70]]];
  const tables=metadata.map(([figure_label,page_numbers])=>({figure:{_currentAsset:{
    id:figure_label,paper_id:'paper',file_id:'si',asset_key:'a077-table-'+figure_label.toLowerCase(),
    figure_label,page_numbers,caption:'Table '+figure_label+'. Original caption.',
  }},text:null}));
  const groups=c.groupTableEntries({tables,remainingTables:[]});
  assert.deepEqual(Array.from(groups,group=>group.records[0].label),
    Array.from({length:18},(_,index)=>'s'+(index+1)));
  assert.deepEqual(Array.from(groups[12].records[0].asset.page_numbers),[71,72]);
});
test('bare table labels support main/SI, case and whitespace without guessing prose or figure labels',()=>{
  const c=setup();
  const tables=['s10','2','  s2  ','10','S1','1'].map(label=>({figure:{_currentAsset:{
    paper_id:'paper',file_id:/s/i.test(label)?'si':'main',figure_label:label,page_numbers:[1],
  }}}));
  const groups=c.groupTableEntries({tables,remainingTables:[]});
  assert.deepEqual(Array.from(groups,group=>group.records[0].label),['1','2','10','s1','s2','s10']);
  assert.equal(c.visualLabelKey('Results for S2 catalysts','table'),null);
  assert.equal(c.visualLabelKey('2.0','table'),null);
  assert.equal(c.visualLabelKey('S2','figure'),null);
});
