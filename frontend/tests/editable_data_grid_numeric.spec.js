const {test, expect} = require('@playwright/test');
const path = require('path');
const fs = require('fs');
const script = process.env.EDG_SCRIPT_PATH || path.resolve(__dirname, '../shared/editable-data-grid.js');

const cases = [
  ['10⁻⁵', 1e-5], ['−2.40', -2.4], ['+2.40', 2.4], ['-0.00', 0], ['−0.00', 0], ['+0.00', 0],
  ['7.2 × 10^7', 72000000], ['−7.2·10⁻⁷', -7.2e-7], ['10^-5', 1e-5], ['10 ^ −5', 1e-5],
  ['7.20E−7', 7.2e-7], ['7.20e+7', 72000000], ['7.20 × 10⁺⁷', 72000000], ['−.50', -.5],
  ['0.12345678901234567890123456789', .12345678901234568],
  ['1.234567890123456789012345678901 × 10^2', 123.45678901234568],
  ['4.9406564584124654 × 10^−324', 5e-324], ['1e-320', 1e-320], ['1.7976931348623157e308', Number.MAX_VALUE],
  ['9007199254740993', 9007199254740992], ['0e999999', 0], ['−0 × 10^−999999', 0],
  ['1e000001', 10], [' \t−2.40 ', -2.4], ['\u00851.20\u0085', 1.2], ['1 ×\u008510^2', 100],
  ...['10−5', '10-5', '1–2', '1-2', '1~2', '≤1', '>1', '≈1', '1 ± 0.1', '1.00s', '1 eV',
    '7.2 x 10^7', '7.2*10**7', '7.2 × 10-5', '10⁻5', '10^⁻⁵', '10^2+3', '(2)', '2/3',
    '1_000', 'NaN', 'Infinity', '１.２', '1e1000000', '1e0000001', '1e-999', '1e309',
    '1.7976931348623159e308', '2.4703282292062327 × 10^−324', '', '\ufeff1', '1\ufeff',
    '1 ×\ufeff10^2', '__import__("os")'].map(raw => [raw, null]),
];

async function openEditor(page, raw='10⁻⁵', numeric=1e-5) {
  const writes = [];
  const source = {id:'source',file_id:'file',asset_id:'asset',source_key:'source-key',source_kind:'table',
    page_number:4,label:'Table 2',table_row:1,table_column:2,quote:raw};
  await page.route('**/*', route => {
    const req = route.request(), url = new URL(req.url());
    if (url.origin !== 'http://127.0.0.1:4173') throw Error('Unexpected external request');
    if (url.pathname.endsWith('/files')) return route.fulfill({json:{items:[{id:'file',role:'si',original_filename:'synthetic.pdf'}]}});
    if (url.pathname.endsWith('/values/value')) {
      if (req.method() === 'PUT') writes.push(req.postDataJSON());
      return route.fulfill({json:{id:'value',conflict:{}}});
    }
    if (url.pathname === '/numeric-fixture') return route.fulfill({contentType:'text/html',body:'<div id="grid"></div>'});
    throw Error('Unmocked route: '+url.pathname);
  });
  await page.goto('http://127.0.0.1:4173/numeric-fixture');
  await page.addScriptTag({path:script});
  await page.evaluate(({raw,numeric,source}) => {
    const row = {id:'row',paper_id:'paper',material:'Synthetic',data_type:'dft',condition:{},identity_status:'confirmed',
      values:[{id:'value',field_name:'adsorption_energy_ev',raw_value:raw,numeric_value:numeric,unit:'eV',
        precision_digits:5,value_type:'explicit',preferred_source_id:'source',sources:[source],value_revision:'a'.repeat(64)}]};
    EditableDataGrid.mount(document.getElementById('grid'), {rows:[row],reload:async()=>{window.reloaded=true;}});
  }, {raw,numeric,source});
  await page.locator('[data-edg-value]').first().click();
  await expect(page.locator('[name=raw_value]')).toHaveValue(raw);
  return {writes,source};
}

async function inputRaw(page, raw) {
  return page.evaluate(raw => {
    const input=document.querySelector('[name=raw_value]');
    input.value=raw;input.dispatchEvent(new Event('input',{bubbles:true}));
    return {raw:input.value,numeric:document.querySelector('[name=numeric_value]').value};
  },raw);
}

test('identical Unicode raw input keeps numeric and exact mock save payload', async ({page}, testInfo) => {
  const {writes,source} = await openEditor(page);
  const before = await page.locator('[name=numeric_value]').inputValue();
  const after = await inputRaw(page,'10⁻⁵');
  expect(Number(before)).toBe(1e-5);
  expect(Number(after.numeric)).toBe(1e-5);
  expect(after.numeric).not.toBe('');
  await expect(page.locator('[name=numeric_value]')).toHaveAttribute('readonly','');
  await page.locator('[name=correction_reason]').fill('synthetic identical raw');
  await page.getByRole('button',{name:'保存修改'}).click();
  await expect(page.locator('dialog')).toHaveCount(0);
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({raw_value:'10⁻⁵',numeric_value:1e-5,precision_digits:5,unit:'eV',
    expected_revision:'a'.repeat(64),source:Object.fromEntries(Object.entries(source).filter(([k])=>k!=='id'))});
  expect(await page.evaluate(()=>window.reloaded)).toBe(true);
  await testInfo.attach('mock-save-payload',{body:JSON.stringify(writes[0],null,2),contentType:'application/json'});
});

test('projection grammar, single-rounding and nonzero underflow match contract in real editor', async ({page}, testInfo) => {
  await openEditor(page);
  const results=[];
  for (const [raw,expected] of cases) {
    const result=await inputRaw(page,raw);
    expect(result.raw).toBe(raw);
    expect(result.numeric, JSON.stringify(raw)).toBe(expected === null ? '' : String(expected));
    results.push({...result,expected});
  }
  // One-step conversion differs from converting the coefficient then multiplying.
  expect(Number('1.234567890123456789012345678901') * 100).not.toBe(123.45678901234568);
  await testInfo.attach('editor-projections',{body:JSON.stringify(results,null,2),contentType:'application/json'});
  // Optional corpus supplied by a no-network backend Decimal->float oracle.
  if (process.env.EDG_NUMERIC_ORACLE) {
    const oracle=JSON.parse(fs.readFileSync(process.env.EDG_NUMERIC_ORACLE,'utf8'));
    for (const {raw,expected} of oracle) {
      const result=await inputRaw(page,raw);
      expect(result.numeric,JSON.stringify(raw)).toBe(expected === null ? '' : String(expected));
    }
    await testInfo.attach('backend-oracle-count',{body:JSON.stringify({compared:oracle.length}),contentType:'application/json'});
  }
});

test('unsupported raw clears stale number but mock save preserves text, precision, unit and source', async ({page}, testInfo) => {
  for (const raw of ['10−5','1.00s','1e-999','1e309']) {
    const {writes,source}=await openEditor(page, '−2.40',-2.4);
    await inputRaw(page,raw);
    await expect(page.locator('[name=numeric_value]')).toHaveValue('');
    await page.locator('[name=correction_reason]').fill('synthetic uncertain raw');
    await page.getByRole('button',{name:'保存修改'}).click();
    await expect(page.locator('dialog')).toHaveCount(0);
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({raw_value:raw,numeric_value:null,precision_digits:5,unit:'eV',
      source:Object.fromEntries(Object.entries(source).filter(([k])=>k!=='id'))});
    await testInfo.attach('unsupported-'+raw,{body:JSON.stringify(writes[0],null,2),contentType:'application/json'});
    await page.unrouteAll({behavior:'wait'});
  }
});


test('supported edited raw saves exact text and source with high precision, tiny values and signed zero', async ({page}) => {
  for (const [raw,number] of [['−2.40',-2.4],['1.234567890123456789012345678901 × 10^2',123.45678901234568],
                            ['4.9406564584124654 × 10^−324',5e-324],['−0.00',0]]) {
    const {writes,source}=await openEditor(page);
    await inputRaw(page,raw);
    await page.locator('[name=correction_reason]').fill('synthetic supported raw');
    await page.getByRole('button',{name:'保存修改'}).click();
    await expect(page.locator('dialog')).toHaveCount(0);
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({raw_value:raw,numeric_value:number,precision_digits:5,unit:'eV',
      source:Object.fromEntries(Object.entries(source).filter(([k])=>k!=='id'))});
    await page.unrouteAll({behavior:'wait'});
  }
});
