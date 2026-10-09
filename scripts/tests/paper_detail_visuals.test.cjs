const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../frontend/pages/paper_detail/index.html'), 'utf8');
function functionSource(name) {
  const start = html.indexOf(`    function ${name}(`);
  assert(start >= 0, `missing ${name}`);
  const rest = html.slice(start);
  const next = rest.slice(1).search(/\n    (?:async )?function /);
  return next < 0 ? rest : rest.slice(0, next + 1);
}
function sandbox() {
  const original = {id:'original',paper_id:'paper',figure_label:'fig_1',page:3,image_path:'clean.png',reading_explanation:{detailed_explanation_zh:'材料结构解读'}};
  const asset = {id:'current',paper_id:'paper',file_id:'main',asset_type:'figure',figure_label:'Fig.1',page_numbers:[3],image_path:'margin.png'};
  const state = {paperId:'paper',paper:{source_pdf_sha256:'sha',figures:[original],tables:[]},rebuildFiles:[{id:'main',paper_id:'paper',role:'main',sha256:'sha'}],rebuildAssets:[asset],rebuildRows:[]};
  const context = vm.createContext({state,console,esc: x=>String(x??''),currentSourceFile:id=>state.rebuildFiles.find(f=>f.id===id),currentPdfLinks:()=>'',storedText:String});
  for(const name of ['isPlaceholderReading','hasEstablishedReading','currentAssetFigure','supersededCurrentAsset','visualLabelKey','sameMainVisual','originalFigureUsable','visualInventory','renderCurrentScientificReading','renderFigureReadingHtml']) vm.runInContext(functionSource(name),context);
  return {context,state,original,asset,inventory:()=>vm.runInContext('visualInventory()',context)};
}
test('same PDF, full figure and page display one clean crop; alternate remains accessible',()=>{
 const s=sandbox(), inv=s.inventory();assert.equal(inv.figures.length,1);assert.equal(inv.figures[0].image_path,'clean.png');assert.equal(inv.alternateFigures[0]._currentAsset.id,'current');
});
test('matching number never merges another PDF edition, SI, page, subfigure or paper',()=>{
 for(const mutate of [s=>s.state.rebuildFiles[0].sha256='other',s=>s.state.rebuildFiles[0].role='si',s=>s.asset.page_numbers=[4],s=>s.asset.subfigure_label='a',s=>s.asset.paper_id='foreign',s=>s.original.paper_id='foreign',s=>delete s.original.paper_id,s=>s.state.paper.source_pdf_sha256=null]) {
  const s=sandbox(); mutate(s);const inv=s.inventory();assert.equal(inv.figures.length,2);assert.equal(inv.alternateFigures.length,0);
 }
});
test('missing original uses current image without reusing original reading',()=>{
 const s=sandbox();s.original.flags=['missing_image_file'];const inv=s.inventory();assert.equal(inv.figures[0]._currentAsset.id,'current');assert.equal(inv.figures[0].reading_explanation,undefined);
});
test('table crops and SI transcripts stay outside image gallery; superseded table stays historical',()=>{
 const s=sandbox();s.state.paper.tables=[{id:'t',paper_id:'paper',caption:'Table 1 Comparison',page:6},{id:'si-t',paper_id:'si-paper',caption:'Table S1 Comparison',page:2}];
 s.state.rebuildAssets.push({...s.asset,id:'t-current',asset_type:'table',figure_label:'Table 1',page_numbers:[6]}, {...s.asset,id:'t-history',asset_type:'table',provenance:{lifecycle:'superseded'}});
 const inv=s.inventory();assert.equal(inv.figures.length,1);assert.equal(inv.tables.length,1);assert.equal(inv.tables[0].text.id,'t');assert.equal(inv.remainingTables.length,1);assert.equal(inv.remainingTables[0].id,'si-t');assert.equal(inv.historicalTables.length,1);
});
test('only explicit lifecycle markers are historical',()=>{
 const s=sandbox();s.asset.version=999;s.asset.asset_key='Fig1_v1';assert.equal(s.inventory().historicalFigures.length,0);
 s.asset.provenance={superseded_by_asset_id:'replacement'};assert.equal(s.inventory().historicalFigures.length,1);
});
test('stored and bound readings keep core and evidence inside collapsed details',()=>{
 const s=sandbox();const reading={type:'ai_reading',core:'LONG CORE',summary:'SHORT',subfigures:[],locators:[],uncertainties:[]};
 s.context.reading=reading;let result=vm.runInContext('renderFigureReadingHtml(reading)',s.context);assert.match(result,/<details class="pd-reading-details">/);assert(result.indexOf('LONG CORE')>result.indexOf('<details'));assert(!/<details[^>]*\bopen\b/.test(result));
 const current={type:'current_scientific',asset:s.asset,summary:'SHORT',core:'LONG CURRENT',subfigures:[],locators:[],uncertainties:[],chinese:true};s.context.reading=current;
 result=vm.runInContext('renderFigureReadingHtml(reading)',s.context);assert(result.indexOf('LONG CURRENT')>result.indexOf('<details'));assert(!/<details[^>]*\bopen\b/.test(result));
});
