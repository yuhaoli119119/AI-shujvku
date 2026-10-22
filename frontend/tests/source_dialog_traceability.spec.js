const {test,expect}=require('@playwright/test');
const fs=require('fs'),path=require('path'),vm=require('vm');
// Optional immutable artifact root is used to record the deployed code's behavior.
const root=process.env.TRACEABILITY_SOURCE_ROOT || path.resolve(__dirname,'..');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const uuid='3a0a46bd-ebf5-46a5-95e8-63b332d06bfa',fid='4188b8eb-c9d8-491c-bf99-67361fcedc98';
const sha='a'.repeat(64),reading='b'.repeat(64);
const locator='word/document.xml#body-child[117]/w:tbl[1]/w:tr[23]/w:tc[19]';
const file={id:fid,paper_id:'p',sha256:sha,original_filename:'SI.docx',role:'si'};
const xml={id:uuid,file_id:fid,paper_id:'p',source_kind:'table',label:'Table S1',page_basis:'ooxml',source_sha256:sha,reading_pdf_sha256:null,page_number:null,table_row:0,table_column:2147483647,ooxml_locator:locator,quote:'<synthetic quote>'};
function extract(text,name){const start=text.indexOf(`function ${name}(`);if(start<0)throw Error(name);const tail=text.slice(start);const end=tail.slice(1).search(/\n(?:    )?(?:async )?function /);return end<0?tail:tail.slice(0,end+1);}
function context(){const c=vm.createContext({encodeURIComponent,URLSearchParams,console});c.window=c;vm.runInContext(read('shared/editable-data-grid.js'),c);Object.assign(c,{esc:v=>String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),storedText:String,currentSourceFile:id=>id===fid?file:null,currentDisplayRole:f=>f.role,state:{rebuildAssets:[]}});c.escapeHtml=c.esc;for(const name of ['currentPdfLinks','renderCurrentSource'])vm.runInContext(extract(read('pages/paper_detail/index.html'),name),c);for(const name of ['validSourcePage','sourcePdfLink','showSource'])vm.runInContext(extract(read('pages/data_table/page.js'),name),c);return c;}
for(const route of ['shared grid / value editor','detail current source','data table legacy source dialog'])test(route+' exposes full UUID, independent coordinates and exact XML locator',async({page})=>{
 const c=context(),before=JSON.stringify(xml);let html;
 if(route.startsWith('shared'))html=c.EditableDataGrid.sourceHtml(xml,[file],'p','p');
 else if(route.startsWith('detail'))html=c.renderCurrentSource(xml);
 else {const nodes={sourceTitle:{},sourceBody:{},sourceModal:{classList:{add(){}}}};Object.assign(c,{$:id=>nodes[id],state:{rows:[{id:'r',paper_id:'p',material:'Synthetic',values:[{field_name:'x',sources:[xml]}]}]},savedValueText:()=>'-1.230',hasConflict:()=>false,fillCorrectionForm:()=>{}});c.showSource('r','x');html=nodes.sourceBody.innerHTML;}
 await page.setContent(html);const visible=await page.locator('body').innerText();expect(visible).toContain(uuid);expect(visible).toContain('table_row');expect(visible).toContain('2147483647');expect(visible).toContain(locator);expect(visible).not.toContain('第 null 页');expect(visible).not.toContain('第 0 页');await expect(page.locator('a[href*="/preview"]')).toHaveCount(0);expect(JSON.stringify(xml)).toBe(before);
 // A column-only source must retain its coordinate; zero is a real row index.
 const column={...xml,table_row:null,table_column:0};const h=c.EditableDataGrid.sourceHtml(column,[file],'p','p');expect(h).toContain('table_column');expect(h).toContain('0');
});
test('derived navigation and read text explicitly identify preview positions, including nested and old Word imports',()=>{
 const c=context();for(const identity of [{page_basis:'derived_pdf',source_sha256:sha,reading_pdf_sha256:reading},{page_identity:{page_basis:'derived_pdf',source_sha256:sha,reading_pdf_sha256:reading}}]){const s={...identity,file_id:fid,paper_id:'p',page_number:4};for(const h of [c.EditableDataGrid.sourceHtml(s,[file],'p','p'),c.currentPdfLinks(fid,[4],s),c.sourcePdfLink({paper_id:'p'},s)]){expect(h).toContain('预览位置');expect(h).toContain('非原件物理页');expect(h).toContain('#page=4');}}
 const legacy={provenance:{source_word_sha256:reading,source_sha256:reading,reading_pdf_sha256:sha}};
 const pdf={...file,original_filename:'registered-reading.pdf'};c.currentSourceFile=()=>pdf;for(const h of [c.currentPdfLinks(fid,[4],legacy),c.EditableDataGrid.sourceHtml({...legacy,file_id:fid,page_number:4},[pdf],'p','p')])expect(h).toContain('预览位置');
});
test('missing pages never fabricate navigation anchors or labels; native PDF navigation remains',()=>{
 const c=context(),pdf={...file,original_filename:'main.pdf'};c.currentSourceFile=()=>pdf;
 for(const page_number of [null,undefined,'',0,-1]){const s={file_id:fid,paper_id:'p',page_number};for(const h of [c.EditableDataGrid.sourceHtml(s,[pdf],'p','p'),c.currentPdfLinks(fid,[page_number],s),c.sourcePdfLink({paper_id:'p'},s)]){expect(h).not.toMatch(/#page=(?:0|1|null|undefined)(?:["&]|$)/);expect(h).not.toMatch(/第 (?:0|1|null|undefined) 页/);}}
 expect(c.currentPdfLinks(fid,[7],{})).toContain('#page=7');expect(c.EditableDataGrid.sourceHtml({file_id:fid,page_number:7},[pdf],'p','p')).toContain('PDF 第 7 页');
});
test('unverified and escaped XML coordinates remain visible without enabling a preview',async({page})=>{
 const c=context(),s={...xml,source_sha256:reading,ooxml_locator:locator+'<img src=x onerror=alert(1)>'};await page.setContent(c.EditableDataGrid.sourceHtml(s,[file],'p','p'));await expect(page.locator('body')).toContainText(s.ooxml_locator);await expect(page.locator('img')).toHaveCount(0);await expect(page.locator('a[href*="/preview"]')).toHaveCount(0);
});

test('table pointers and section read text keep XML/null and derived positions separate',async({page})=>{
 const c=context(),detail=read('pages/paper_detail/index.html');vm.runInContext(extract(detail,'renderSectionEvidence'),c);vm.runInContext(extract(detail,'renderTableGroup'),c);
 const section={id:'section',provenance:{file_id:fid,page_basis:'derived_pdf',sources:[{page_number:4,locator:'Synthetic section'}, {page_number:null,locator:'Unpaged section'}]}};
 const sectionHtml=c.renderSectionEvidence(section);expect(sectionHtml).toContain('预览位置 4（非原件物理页）');expect(sectionHtml).not.toContain('#page=null');expect(sectionHtml).not.toContain('物理页 4');
 Object.assign(c,{API_BASE:'/api/papers',renderTableBody:()=>'<table></table>',renderFigureReadingHtml:()=>'',currentScientificReading:()=>({})});
 const tableHtml=c.renderTableGroup({key:'table',records:[{sourceId:'p',asset:{id:'a',file_id:fid,figure_label:'Table S1',page_numbers:[],table_content:{sources:[xml]}},content:{}}]});
 await page.setContent(tableHtml);await page.locator('details').evaluate(el=>el.open=true);await expect(page.locator('body')).toContainText(locator);await expect(page.locator('body')).toContainText(uuid);await expect(page.locator('body')).not.toContainText('第 null 页');
});

test('actual value dialog displays full source fields and correct accessible preview label',async({page},testInfo)=>{
 await page.goto('http://127.0.0.1:4173/pages/data_table/index.html');await page.setContent('<div></div>');await page.addStyleTag({path:path.join(root,'shared/editable-data-grid.css')});await page.addScriptTag({path:path.join(root,'shared/editable-data-grid.js')});
 await page.route('**/api/rebuild/papers/p/files',r=>r.fulfill({json:{items:[file]}}));await page.route('**/api/rebuild/values/v',r=>{expect(r.request().method()).toBe('GET');return r.fulfill({json:{conflict:{}}});});
 await page.evaluate(async source=>{await EditableDataGrid.editValue({id:'r',paper_id:'p',material:'Synthetic'},{id:'v',field_name:'adsorption_energy_ev',raw_value:'-1.230',numeric_value:-1.23,unit:'eV',value_type:'explicit',sources:[source]},{});},xml);
 await expect(page.locator('dialog')).toBeVisible();await expect(page.locator('.edg-source')).toContainText(uuid);await expect(page.locator('.edg-source')).toContainText('table_row（从0计）：0');await expect(page.locator('.edg-source')).toContainText('table_column（从0计）：2147483647');await expect(page.locator('.edg-source code')).toHaveText(locator);await expect(page.getByLabel('页码（OOXML 原件无物理页，保持留空）',{exact:true})).toHaveValue('');await expect(page.locator('a[href*="/preview"]')).toHaveCount(0);
 await page.screenshot({path:testInfo.outputPath('pageless-source-dialog.png')});await page.getByRole('button',{name:'关闭'}).click();
 await page.evaluate(async source=>{await EditableDataGrid.editValue({id:'r',paper_id:'p',material:'Synthetic'},{id:'v',field_name:'adsorption_energy_ev',value_type:'explicit',sources:[source]},{});},{...xml,page_basis:'derived_pdf',ooxml_locator:null,page_number:4,reading_pdf_sha256:reading});await expect(page.getByLabel('预览位置（非原件物理页）',{exact:true})).toHaveValue('4');await expect(page.locator('.edg-source')).toContainText('预览位置 4（非原件物理页）');
});

test('long nested OOXML locators wrap without truncation and retain nonphysical labels',async({page})=>{
 const c=context(),long=locator+'/w:p[1]'.repeat(400),s={...xml,page_basis:null,ooxml_locator:null,page_identity:{page_basis:'ooxml',source_sha256:sha,ooxml_locator:long}};
 await page.setViewportSize({width:390,height:844});await page.setContent(c.EditableDataGrid.sourceHtml(s,[file],'p','p'));await expect(page.locator('code').last()).toHaveText(long);await expect(page.locator('.edg-source')).toContainText('OOXML 定位（DOCX 原件，无物理页）');expect(await page.locator('.edg-source-trace').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true);await expect(page.locator('a[href*="/preview"]')).toHaveCount(0);
 const invalid=c.sourcePdfLink({paper_id:'p'},{...xml,file_id:'invalid'});expect(invalid).toContain('文件标注无效');expect(invalid).not.toContain('<a');
});
