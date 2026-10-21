const {test,expect}=require('@playwright/test');
const fs=require('fs'),path=require('path'),vm=require('vm');
const read=p=>fs.readFileSync(path.join(__dirname,'..',p),'utf8');
function fn(source,name){const start=source.search(new RegExp('(?:async )?function '+name+'\\('));if(start<0)throw Error(name);const rest=source.slice(start),next=rest.slice(1).search(/\n(?:    )?(?:async )?function /);return next<0?rest:rest.slice(0,next+1);}
const old={id:'old',paper_id:'paper',role:'main',sha256:'a'.repeat(64),original_filename:'old.pdf',lifecycle_status:'superseded',preferred:false,scientific_review_required:true};
const main={...old,id:'new',original_filename:'new.pdf',lifecycle_status:'active',preferred:true,scientific_review_required:false};
const si={...main,id:'si',role:'si',original_filename:'si.pdf',preferred:false};
test('source picker chooses active preferred main and SI while history retains old UUID',()=>{
  const elements={fileList:{},cropFile:{}};
  const ctx=vm.createContext({state:{files:[old,main,si]},$:id=>elements[id],encodeURIComponent});
  const script=read('pages/figure_assets/page.js');
  vm.runInContext(fn(script,'assetText'),ctx);vm.runInContext(fn(script,'renderFiles'),ctx);ctx.renderFiles();
  expect(elements.fileList.innerHTML).toContain('/files/old/preview');
  expect(elements.fileList.innerHTML).toContain('已被替代（历史）');
  expect(elements.fileList.innerHTML).toContain('需独立科学复核');
  expect(elements.cropFile.innerHTML).not.toContain('value="old"');
  expect(elements.cropFile.innerHTML).toContain('value="new"');
  expect(elements.cropFile.innerHTML).toContain('value="si"');
});
test('old scientific source remains on its original UUID and page with independent review label',()=>{
  const ctx=vm.createContext({encodeURIComponent});ctx.window=ctx;
  vm.runInContext(read('shared/editable-data-grid.js'),ctx);
  const source={id:'source',file_id:'old',paper_id:'paper',page_number:4,source_kind:'text',quote:'saved quote'},before=JSON.stringify(source);
  const markup=ctx.EditableDataGrid.sourceHtml(source,[old,main],'paper','paper');
  expect(markup).toContain('/files/old/preview');expect(markup).toContain('#page=4');expect(markup).not.toContain('/files/new/preview');
  expect(markup).toContain('需独立科学复核');expect(JSON.stringify(source)).toBe(before);
});
test('file history exposes preferred and superseded source and escapes filenames',()=>{
  const ctx=vm.createContext({state:{files:{items:[{...old,original_filename:'<old>.pdf'},main]}},encodeURIComponent});
  const script=read('pages/literature_library/rebuild.js');vm.runInContext(fn(script,'esc'),ctx);vm.runInContext(fn(script,'fileHistoryMarkup'),ctx);
  const markup=ctx.fileHistoryMarkup();expect(markup).toContain('当前首选');expect(markup).toContain('已被替代（历史）');expect(markup).toContain('&lt;old&gt;.pdf');expect(markup).not.toContain('<old>');
});
