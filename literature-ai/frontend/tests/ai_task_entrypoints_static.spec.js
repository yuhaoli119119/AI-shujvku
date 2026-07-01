const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');

function readFrontendFile(relativePath) {
  return fs.readFileSync(path.join(REPO_ROOT, relativePath), 'utf8');
}

test('review center is the formal AI prompt entrypoint surface', () => {
  const reviewCenter = readFrontendFile('pages/review_center/index.html');
  const dftReviewerScope = reviewCenter.slice(
    reviewCenter.indexOf('      dft: {'),
    reviewCenter.indexOf('      dft_primary: {')
  );
  const dftPrimaryScope = reviewCenter.slice(
    reviewCenter.indexOf('      dft_primary: {'),
    reviewCenter.indexOf('    const conflictState')
  );

  expect(reviewCenter).toContain('主文图片审核提示词');
  expect(reviewCenter).toContain('支撑文献图片审核提示词');
  expect(reviewCenter).toContain('表格审核提示词');
  expect(reviewCenter).toContain('DFT 普通 AI 审核提示词');
  expect(reviewCenter).toContain('DFT 主 AI 判断/修复提示词');
  expect(reviewCenter).toContain('本轮唯一目标：只核验 DFT 数据。');
  expect(reviewCenter).toContain('本轮允许写入的 target_type 只有 dft_results');
  expect(reviewCenter).toContain('如果准备写入非 dft_results，立即停止');
  expect(reviewCenter).toContain('必须读取主文与已关联 SI 的 DFT 文本/表格证据');
  expect(reviewCenter).toContain('DFT 漏项常在普通 tables 或 PDF 表格里，不能跳过');
  expect(reviewCenter).toContain('禁止修改 figure、writing_card、mechanism_claim、metadata、普通表格对象');
  expect(reviewCenter).toContain('不再要求切换 dft_primary_repair key');
  expect(reviewCenter).toContain('repair_dft_audit_issues_batch(paper_id=<当前 paper_id>, auto_finalize=true)');
  expect(reviewCenter).toContain('issue_count=0 或 candidate 为 0 条审核意见都不是阻塞原因');
  expect(reviewCenter).toContain('0 条审核意见都不是阻塞原因');
  expect(reviewCenter).toContain('批量失败只重试失败项');
  expect(reviewCenter).not.toContain('blocked_by_missing_primary_repair_identity');
  expect(dftReviewerScope).toContain('你的角色：DFT 数据审核员');
  expect(dftReviewerScope).not.toContain('主 AI');
  expect(dftReviewerScope).not.toContain('数据处理员');
  expect(dftPrimaryScope).toContain('你的角色：DFT 数据处理员');
  expect(dftPrimaryScope).not.toContain('普通 AI');
  expect(dftPrimaryScope).not.toContain('审核员');
  expect(reviewCenter).not.toContain('前置门可进入且存在 DFT candidates 时必须逐条写入 PASS/REVISE/REJECT/NEEDS_HUMAN');
  expect(reviewCenter).toContain('一次只能选择一个目标');
  expect(reviewCenter).toContain('return actionConfig.scopeNote + "\\n\\n" + rendered;');
  expect(reviewCenter).toContain('const template = profileTemplates[kind] || templates[kind] || compositeTemplates[kind];');
  expect(reviewCenter).not.toContain('|| templates.overall');
  expect(reviewCenter).not.toContain('<option value="figure">图表指令</option>');
  expect(reviewCenter).not.toContain('resolveVisualPromptKind()');
});

test('detail pages no longer expose formal prompt copy entrypoints', () => {
  const reviewJs = readFrontendFile('pages/literature_library/review.js');
  const dftWorkflow = readFrontendFile('pages/literature_library/dft-workflow.js');
  const reviewCards = readFrontendFile('pages/literature_library/review-card-renderers.js');
  const combined = [reviewJs, dftWorkflow, reviewCards].join('\n');

  expect(combined).not.toContain('复制总体解析指令');
  expect(combined).not.toContain('总体解析指令</summary>');
  expect(combined).not.toContain('生成下一轮 AI 审核任务');
  expect(combined).toContain('请回审核中心按单篇文献发起 AI 审核任务');
});

test('DFT audit center is not a daily primary prompt entrypoint', () => {
  const topnav = readFrontendFile('shared/topnav.js');
  const auditCenter = readFrontendFile('pages/dft_audit_center/index.html');

  expect(topnav).not.toContain('label: "DFT 核验"');
  expect(auditCenter).not.toContain('copyQueueHintBtn');
  expect(auditCenter).not.toContain('复制主 AI 处理提示');
  expect(auditCenter).toContain('日常 DFT 主 AI 判断/修复提示词必须回审核中心选择一篇主文献后复制');
});
