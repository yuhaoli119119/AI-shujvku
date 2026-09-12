import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';

const BASE_URL = process.env.TEST_BASE_URL || process.env.PLAYWRIGHT_TEST_BASE_URL || 'http://127.0.0.1:4173';

function jsonResponse(route, data, status = 200, headers = {}) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...headers },
    body: JSON.stringify(data)
  });
}

test.describe('审核中心离线闭环与契约边界 (Review Center Offline Handoff & Contract Boundary)', () => {

  test('静态代码检查：前端不含 verification-sessions、human-confirm、accept-ai、manual-decision 及 finalizeWebAiEvidenceReview', async () => {
    const pageJsPath = path.resolve(__dirname, '../pages/review_center/page.js');
    const pageJs = fs.readFileSync(pageJsPath, 'utf8');

    expect(pageJs).not.toContain('verification-sessions');
    expect(pageJs).not.toContain('human-confirm');
    expect(pageJs).not.toContain('/accept-ai');
    expect(pageJs).not.toContain('/manual-decision');
    expect(pageJs).not.toContain('executeAcceptAiAdjudication');
    expect(pageJs).not.toContain('executeDraftCorrection');
    expect(pageJs).not.toContain('executeManualConflictDecision');
    expect(pageJs).not.toContain('humanConfirm(');

    const returnJsPath = path.resolve(__dirname, '../pages/review_center/web-ai-return.js');
    const returnJs = fs.readFileSync(returnJsPath, 'utf8');
    expect(returnJs).not.toContain('finalizeWebAiEvidenceReview');
    expect(returnJs).not.toContain('webAiFinalizeEvidenceBtn');

    const htmlPath = path.resolve(__dirname, '../pages/review_center/index.html');
    const html = fs.readFileSync(htmlPath, 'utf8');
    expect(html).not.toContain('webAiFinalizeEvidenceBtn');
  });

  test('网络与安全边界：页面交互不发起 verification-sessions、human-confirm、accept-ai 或 manual-decision', async ({ page }) => {
    const forbiddenRequests = [];

    page.on('request', req => {
      const u = req.url();
      if (
        u.includes('verification-sessions') ||
        u.includes('human-confirm') ||
        u.includes('accept-ai') ||
        u.includes('manual-decision')
      ) {
        forbiddenRequests.push({ url: u, method: req.method() });
      }
    });

    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/libraries') {
        return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      }
      if (pathname === '/api/health') {
        return jsonResponse(route, { active_library: '锂硫双原子' });
      }
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [
            {
              paper_id: 'paper-boundary-001',
              paper_code: 'B001',
              title: 'Boundary Check Paper',
              year: 2025,
              created_at: '2026-09-01T12:00:00Z',
              workflow_status: 'Imported',
              dft_review_conflict_count: 1,
              supplementary_group: { role: 'main', support_papers: [] }
            }
          ]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, {
          rows: [
            {
              conflict_id: 'conf-b1',
              paper_id: 'paper-boundary-001',
              target_type: 'dft_results',
              target_id: 'dft-res-b1',
              field_name: '吸附能 (E_ads)',
              affected_field_names: ['吸附能 (E_ads)'],
              target_summary: {
                current_value: '-2.10',
                current_unit: 'eV',
                object_label: 'Boundary target',
                property_type: 'adsorption_energy'
              },
              opinions: [
                {
                  source_label: 'Agent-A',
                  decision: 'modify',
                  value: '-2.15',
                  unit: 'eV',
                  reason: 'PDF 表2数据为 -2.15 eV',
                  anchor_summary: {
                    page: 3,
                    table: 'Table 2',
                    locator_status: 'exact_page',
                    quoted_text: 'E_ads = -2.15 eV'
                  },
                  evidence: {
                    evidence_text: 'E_ads = -2.15 eV',
                    locator: {
                      page: 3,
                      table: 'Table 2',
                      locator_status: 'exact_page'
                    }
                  }
                }
              ],
              adjudication: {
                adjudication_mode: 'manual',
                recommended_action: 'manual_review',
                reason_summary: '多源置信度差异'
              }
            }
          ]
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    // 验证表格行中不存在“确认完整”或“确认”按钮
    const confirmBtn = page.locator('button[title="确认完整"]');
    await expect(confirmBtn).toHaveCount(0);

    // 打开冲突详情弹窗 (点击 conflict chip [data-action="open-conflicts"])
    const conflictChip = page.locator('[data-action="open-conflicts"]');
    await expect(conflictChip).toBeVisible();
    await conflictChip.click();

    // 验证冲突弹窗可见
    await expect(page.locator('#infoOverlay')).toBeVisible();

    // 验证冲突弹窗中不存在直接裁决按钮（accept-ai / adopt-opinion / manual-decision）
    await expect(page.locator('[data-action="accept-ai"]')).toHaveCount(0);
    await expect(page.locator('[data-action="adopt-opinion"]')).toHaveCount(0);
    await expect(page.locator('[data-action="draft-correction"]')).toHaveCount(0);

    // 验证没有任何违规网络请求发出
    expect(forbiddenRequests).toEqual([]);
  });

  test('命令生成与复制：包含完整真实上下文、三态判断逻辑，且无网络写入副作用', async ({ page }) => {
    let mutatingRequestCount = 0;

    page.on('request', req => {
      const m = req.method();
      if (['POST', 'PATCH', 'PUT', 'DELETE'].includes(m)) {
        mutatingRequestCount++;
      }
    });

    await page.addInitScript(() => {
      window.__clipboardText = '';
      navigator.clipboard.writeText = async (text) => {
        window.__clipboardText = text;
        return Promise.resolve();
      };
    });

    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/libraries') {
        return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      }
      if (pathname === '/api/health') {
        return jsonResponse(route, { active_library: '锂硫双原子' });
      }
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [
            {
              paper_id: 'paper-cmd-101',
              paper_code: 'B0101',
              title: 'DFT Adsorption on Dual-Atom Catalysts',
              year: 2025,
              created_at: '2026-09-01T12:00:00Z',
              workflow_status: 'Imported',
              dft_review_conflict_count: 1,
              supplementary_group: { role: 'main', support_papers: [] }
            }
          ]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, {
          rows: [
            {
              conflict_id: 'conf-101',
              paper_id: 'paper-cmd-101',
              target_type: 'dft_results',
              target_id: 'dft-res-uuid-1234',
              field_name: '吸附能 (E_ads)',
              affected_field_names: ['吸附能 (E_ads)'],
              conflict_types: ['value_discrepancy'],
              target_summary: {
                current_value: '-2.10',
                current_unit: 'eV',
                object_label: 'Fe2N6 adsorption',
                property_type: 'adsorption_energy',
                normalized_material: 'Fe2N6'
              },
              opinions: [
                {
                  source_label: 'Agent-Extractor-1',
                  decision: 'modify',
                  value: '-2.18',
                  unit: 'eV',
                  reason: '正文第4页Table 1',
                  anchor_summary: {
                    page: 4,
                    table: 'Table 1',
                    locator_status: 'exact_page',
                    quoted_text: 'Adsorption energy of Li2S6 on Fe2N6 is -2.18 eV'
                  },
                  evidence: {
                    evidence_text: 'Adsorption energy of Li2S6 on Fe2N6 is -2.18 eV',
                    locator: {
                      page: 4,
                      table: 'Table 1',
                      locator_status: 'exact_page'
                    }
                  }
                },
                {
                  source_label: 'Agent-Extractor-2',
                  decision: 'keep',
                  value: '-2.10',
                  unit: 'eV',
                  reason: '摘要提及初始吸附构型',
                  anchor_summary: {
                    page: 1,
                    figure: 'Figure 1',
                    locator_status: 'approximate_page',
                    quoted_text: 'initial binding energy around -2.10 eV'
                  },
                  evidence: {
                    evidence_text: 'initial binding energy around -2.10 eV',
                    locator: {
                      page: 1,
                      figure: 'Figure 1',
                      locator_status: 'approximate_page'
                    }
                  }
                }
              ],
              adjudication: {
                adjudication_mode: 'manual',
                recommended_action: 'manual_review',
                reason_summary: '构型定义不同导致数值差异'
              }
            }
          ]
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    // 打开冲突详情 (点击 [data-action="open-conflicts"])
    await page.locator('[data-action="open-conflicts"]').click();
    await expect(page.locator('#infoOverlay')).toBeVisible();

    // 点击“复制此问题处理命令”
    const copyCmdBtn = page.locator('[data-action="copy-issue-command"]');
    await expect(copyCmdBtn).toBeVisible();
    await copyCmdBtn.click();

    // 验证 Toast 提示
    await expect(page.locator('#toast')).toBeVisible();
    await expect(page.locator('#toast')).toContainText('已复制此问题处理命令');

    // 验证纯只读：无任何网络变更请求
    expect(mutatingRequestCount).toBe(0);

    // 获取复制的命令文本
    const clipboardText = await page.evaluate(() => window.__clipboardText);

    // 验证包含真实元数据且不使用虚构数据
    expect(clipboardText).toContain('paper-cmd-101');
    expect(clipboardText).toContain('B0101');
    expect(clipboardText).toContain('DFT Adsorption on Dual-Atom Catalysts');
    expect(clipboardText).toContain('dft_results');
    expect(clipboardText).toContain('dft-res-uuid-1234');
    expect(clipboardText).toContain('吸附能 (E_ads)');
    expect(clipboardText).toContain('value_discrepancy');

    // 验证复制结果包含 target_summary.current_value/current_unit
    expect(clipboardText).toContain('-2.10 eV');
    // 验证包含 affected_field_names
    expect(clipboardText).toContain('影响字段 (affected_field_names): 吸附能 (E_ads)');

    // 验证全量包含 2 个审核意见的真实契约值
    expect(clipboardText).toContain('Agent-Extractor-1');
    expect(clipboardText).toContain('建议值: -2.18 eV');
    expect(clipboardText).toContain('页码: 4');
    expect(clipboardText).toContain('图表: Table 1');
    expect(clipboardText).toContain('定位状态: exact_page');
    expect(clipboardText).toContain('原文证据: Adsorption energy of Li2S6 on Fe2N6 is -2.18 eV');

    expect(clipboardText).toContain('Agent-Extractor-2');
    expect(clipboardText).toContain('建议值: -2.10 eV');
    expect(clipboardText).toContain('页码: 1');
    expect(clipboardText).toContain('图表: Figure 1');
    expect(clipboardText).toContain('定位状态: approximate_page');
    expect(clipboardText).toContain('原文证据: initial binding energy around -2.10 eV');

    // 验证绝不出现 [object Object]
    expect(clipboardText).not.toContain('[object Object]');

    // 验证三态逻辑明确表述，且无“证据确凿无误就修正入库”的错误表述
    expect(clipboardText).toContain('如果核查后确认当前数据正确：不得修改数据，只回读对象并报告“无需修改”及对应原文证据');
    expect(clipboardText).toContain('如果核查后确认当前数据错误：必须在原文证据充分时，使用该对象对应的现有 MCP/API 工具修正，然后回读验证');
    expect(clipboardText).toContain('如果证据不足或科研含义不能唯一确定：不得修改，如实报告 blocked 阻塞原因及缺失证据');
    expect(clipboardText).not.toContain('证据确凿无误就修正入库');
  });

  test('假契约字段失效验证：用旧的 proposed_value/evidence_location 假字段无法让命令生成有效建议与证据', async ({ page }) => {
    await page.addInitScript(() => {
      window.__clipboardText = '';
      navigator.clipboard.writeText = async (text) => {
        window.__clipboardText = text;
        return Promise.resolve();
      };
    });

    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/libraries') {
        return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      }
      if (pathname === '/api/health') {
        return jsonResponse(route, { active_library: '锂硫双原子' });
      }
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [
            {
              paper_id: 'paper-fake-fields',
              paper_code: 'F001',
              title: 'Legacy Fake Fields Paper',
              year: 2025,
              created_at: '2026-09-01T12:00:00Z',
              workflow_status: 'Imported',
              dft_review_conflict_count: 1,
              supplementary_group: null
            }
          ]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, {
          rows: [
            {
              conflict_id: 'conf-fake',
              paper_id: 'paper-fake-fields',
              target_type: 'dft_results',
              target_id: 'dft-res-fake',
              field_name: '吸附能 (E_ads)',
              // 仅提供旧测试假字段，不提供 target_summary.current_value、opinion.value、opinion.evidence
              current_value: '-9.99 eV',
              opinions: [
                {
                  source_label: 'Fake-Agent',
                  decision: 'modify',
                  proposed_value: '-8.88 eV', // 假字段
                  evidence_location: { // 假字段
                    page: 99,
                    table: 'FakeTable',
                    locator_status: 'fake_locator',
                    evidence_text: 'Fake evidence text that should not appear'
                  }
                }
              ]
            }
          ]
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.locator('[data-action="open-conflicts"]').click();
    await page.locator('[data-action="copy-issue-command"]').click();

    const clipboardText = await page.evaluate(() => window.__clipboardText);

    // 确认旧的 proposed_value 和 evidence_location 假字段未被作为有效数据读取
    expect(clipboardText).toContain('建议值: 后端未提供');
    expect(clipboardText).toContain('页码: 后端未提供');
    expect(clipboardText).toContain('原文证据: 后端未提供');
    expect(clipboardText).not.toContain('-8.88 eV');
    expect(clipboardText).not.toContain('Fake evidence text');
    expect(clipboardText).not.toContain('[object Object]');
  });

  test('缺失字段容错：缺失项显示“后端未提供”，严禁伪造兜底数据', async ({ page }) => {
    await page.addInitScript(() => {
      window.__clipboardText = '';
      navigator.clipboard.writeText = async (text) => {
        window.__clipboardText = text;
        return Promise.resolve();
      };
    });

    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/libraries') {
        return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      }
      if (pathname === '/api/health') {
        return jsonResponse(route, { active_library: '锂硫双原子' });
      }
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [
            {
              paper_id: 'paper-missing-002',
              title: 'Paper Missing Fields',
              year: 2025,
              created_at: '2026-09-01T12:00:00Z',
              workflow_status: 'Imported',
              dft_review_conflict_count: 1,
              supplementary_group: null
            }
          ]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, {
          rows: [
            {
              conflict_id: 'conf-sparse',
              paper_id: 'paper-missing-002',
              target_type: 'dft_results',
              target_id: 'dft-res-sparse',
              field_name: '形成能 (E_form)',
              target_summary: {}, // 缺失 current_value
              opinions: [] // 无意见
            }
          ]
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.locator('[data-action="open-conflicts"]').click();
    await page.locator('[data-action="copy-issue-command"]').click();

    const clipboardText = await page.evaluate(() => window.__clipboardText);
    expect(clipboardText).toContain('当前对象真实值 (current_value): 后端未提供');
    expect(clipboardText).toContain('后端未提供具体审核意见');
    expect(clipboardText).not.toContain('-2.15');
    expect(clipboardText).not.toContain('吸附能');
  });

  test('多冲突独立性与双快照离线闭环：复制命令不改状态，外部处理后刷新页面读取新状态', async ({ page }) => {
    let currentConflicts = [
      {
        conflict_id: 'conf-multi-1',
        paper_id: 'paper-dual-conflict',
        target_type: 'dft_results',
        target_id: 'dft-target-1',
        field_name: '吸附能 (E_ads)',
        affected_field_names: ['吸附能 (E_ads)'],
        target_summary: {
          current_value: '-2.10',
          current_unit: 'eV',
          object_label: 'Target 1',
          property_type: 'adsorption_energy'
        },
        opinions: [{
          source_label: 'Agent-1',
          decision: 'modify',
          value: '-2.15',
          unit: 'eV',
          reason: '测试吸附能',
          anchor_summary: { page: 2, quoted_text: 'E_ads is -2.15 eV' },
          evidence: { evidence_text: 'E_ads is -2.15 eV', locator: { page: 2 } }
        }]
      },
      {
        conflict_id: 'conf-multi-2',
        paper_id: 'paper-dual-conflict',
        target_type: 'dft_results',
        target_id: 'dft-target-2',
        field_name: '过电位 (Overpotential)',
        affected_field_names: ['过电位 (Overpotential)'],
        target_summary: {
          current_value: '0.45',
          current_unit: 'V',
          object_label: 'Target 2',
          property_type: 'overpotential'
        },
        opinions: [{
          source_label: 'Agent-2',
          decision: 'modify',
          value: '0.35',
          unit: 'V',
          reason: '测试过电位',
          anchor_summary: { page: 5, quoted_text: 'Overpotential is 0.35 V' },
          evidence: { evidence_text: 'Overpotential is 0.35 V', locator: { page: 5 } }
        }]
      }
    ];

    let currentPaperRow = {
      paper_id: 'paper-dual-conflict',
      paper_code: 'B0200',
      title: 'Dual Conflict Paper',
      year: 2025,
      created_at: '2026-09-01T12:00:00Z',
      workflow_status: 'Imported',
      dft_review_conflict_count: 2,
      supplementary_group: { role: 'main', support_papers: [] }
    };

    await page.addInitScript(() => {
      window.__clipboardText = '';
      navigator.clipboard.writeText = async (text) => {
        window.__clipboardText = text;
        return Promise.resolve();
      };
    });

    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/libraries') {
        return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      }
      if (pathname === '/api/health') {
        return jsonResponse(route, { active_library: '锂硫双原子' });
      }
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [currentPaperRow]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, { rows: currentConflicts });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    // 1. 第一快照加载：2 个冲突
    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.locator('[data-action="open-conflicts"]').click();

    // 验证弹窗内左侧冲突列表包含 2 项独立冲突
    const conflictItems = page.locator('.conflict-list-item');
    await expect(conflictItems).toHaveCount(2);

    // 用户复制当前选中第一个冲突（E_ads）的处理命令
    await page.locator('[data-action="copy-issue-command"]').click();

    const text1 = await page.evaluate(() => window.__clipboardText);
    expect(text1).toContain('dft-target-1');
    expect(text1).toContain('吸附能 (E_ads)');
    expect(text1).not.toContain('dft-target-2');

    // 切换到第二项冲突（直接点击第二个 conflict-list-item）
    await conflictItems.nth(1).click();
    await page.locator('[data-action="copy-issue-command"]').click();
    const text2 = await page.evaluate(() => window.__clipboardText);
    expect(text2).toContain('dft-target-2');
    expect(text2).toContain('过电位 (Overpotential)');
    expect(text2).not.toContain('dft-target-1');

    // 验证复制后页面两个冲突均完好存在，未改变前端状态
    await expect(conflictItems).toHaveCount(2);

    // 关闭弹窗
    await page.locator('.modal-close').first().click();

    // 2. 模拟外部 AI 处理完成闭环：外部 AI 通过 MCP 修正了 E_ads，后端状态更新为只剩 Overpotential (1 项未决冲突)
    currentConflicts = [
      {
        conflict_id: 'conf-multi-2',
        paper_id: 'paper-dual-conflict',
        target_type: 'dft_results',
        target_id: 'dft-target-2',
        field_name: '过电位 (Overpotential)',
        affected_field_names: ['过电位 (Overpotential)'],
        target_summary: {
          current_value: '0.45',
          current_unit: 'V',
          object_label: 'Target 2',
          property_type: 'overpotential'
        },
        opinions: [{
          source_label: 'Agent-2',
          decision: 'modify',
          value: '0.35',
          unit: 'V',
          reason: '测试过电位',
          anchor_summary: { page: 5, quoted_text: 'Overpotential is 0.35 V' },
          evidence: { evidence_text: 'Overpotential is 0.35 V', locator: { page: 5 } }
        }]
      }
    ];
    currentPaperRow.dft_review_conflict_count = 1;

    // 用户回到页面点击刷新按钮
    await page.locator('button[onclick="loadReviewCenter()"]').click();

    // 再次打开冲突弹窗：验证页面仅根据第二次真实响应更新，只剩下 1 项未决冲突
    await page.locator('[data-action="open-conflicts"]').click();
    await expect(page.locator('.conflict-list-item')).toHaveCount(1);
    await expect(page.locator('.conflict-list-item')).toContainText('过电位 (Overpotential)');
  });

  test('剪贴板 API 与 fallback 均失败时，正确展示失败提示且无未捕获异常', async ({ page }) => {
    let unhandledError = null;
    page.on('pageerror', err => {
      unhandledError = err;
    });

    await page.addInitScript(() => {
      // 模拟 navigator.clipboard 拒绝
      if (navigator.clipboard) {
        navigator.clipboard.writeText = async () => {
          throw new Error('navigator.clipboard write rejected');
        };
      }
      // 模拟 document.execCommand 回退亦失败
      document.execCommand = () => false;
    });

    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/libraries') return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      if (pathname === '/api/health') return jsonResponse(route, { active_library: '锂硫双原子' });
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [
            {
              paper_id: 'paper-clip-fail',
              paper_code: 'CF001',
              title: 'Clipboard Fail Paper',
              year: 2025,
              workflow_status: 'Imported',
              dft_review_conflict_count: 1,
              supplementary_group: null
            }
          ]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, {
          rows: [
            {
              conflict_id: 'conf-cf',
              paper_id: 'paper-clip-fail',
              target_type: 'dft_results',
              target_id: 'dft-res-cf',
              field_name: '吸附能 (E_ads)',
              target_summary: { current_value: '-1.5', current_unit: 'eV' },
              opinions: []
            }
          ]
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.locator('[data-action="open-conflicts"]').click();
    await expect(page.locator('[data-action="copy-issue-command"]')).toBeVisible();

    await page.locator('[data-action="copy-issue-command"]').click();

    // 验证失败 Toast 提示
    await expect(page.locator('#toast')).toBeVisible();
    await expect(page.locator('#toast')).toContainText('复制失败，请重试或手动复制');

    // 验证无未处理异常抛出
    expect(unhandledError).toBeNull();
  });

  test('支持信息 (SI) 优先按 supplementary_group.role 判定', async ({ page }) => {
    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/libraries') return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      if (pathname === '/api/health') return jsonResponse(route, { active_library: '锂硫双原子' });
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 2, total_count: 2, hidden_supplementary_count: 1 },
          rows: [
            {
              paper_id: 'paper-main-001',
              paper_code: 'M001',
              title: 'Main Paper with SI group',
              year: 2025,
              paper_type: 'research',
              workflow_status: 'Imported',
              dft_review_conflict_count: 0,
              supplementary_group: { role: 'main', support_papers: [{ paper_id: 'paper-si-001' }] }
            },
            {
              paper_id: 'paper-si-001',
              paper_code: 'SI001',
              title: 'Supplementary Information Paper',
              year: 2025,
              paper_type: 'research', // paper_type 为普通 research，但 supplementary_group.role 为 supplementary
              workflow_status: 'Imported',
              dft_review_conflict_count: 0,
              supplementary_group: { role: 'supplementary', main_paper_id: 'paper-main-001' }
            }
          ]
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    // 默认开启 hideSupplementaryFilter 时，SI 论文被隐藏
    const rows = page.locator('#rows tr');
    await expect(rows).toHaveCount(1);
    await expect(page.locator('#rows')).toContainText('Main Paper with SI group');
    await expect(page.locator('#rows')).not.toContainText('Supplementary Information Paper');
  });

  test('网页 AI 图表回传人工导入工具：依赖指纹与范围校验，不作为后台自动任务', async ({ page }) => {
    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/libraries') return jsonResponse(route, [{ name: '锂硫双原子', is_active: true }]);
      if (pathname === '/api/health') return jsonResponse(route, { active_library: '锂硫双原子' });
      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: { returned: 1, total_count: 1 },
          rows: [
            {
              paper_id: 'paper-bundle-001',
              paper_code: 'B0301',
              title: 'Bundle Verification Paper',
              year: 2025,
              created_at: '2026-09-01T12:00:00Z',
              workflow_status: 'Imported',
              dft_review_conflict_count: 0,
              supplementary_group: { role: 'main', support_papers: [] }
            }
          ]
        });
      }
      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, { rows: [] });
      }
      if (pathname.includes('/chart-review-task')) {
        return jsonResponse(route, {
          paper_id: 'paper-bundle-001',
          paper_code: 'B0301',
          figure_count: 5,
          table_count: 2,
          runs: []
        });
      }
      return route.fulfill({ status: 200, body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html?paper_id=paper-bundle-001&scope=paper`);

    // 1. 未导出图表证据包时（bundleFingerprint 为空），回传图表 JSON 被拦截并 Toast 提示
    await page.locator('#directEvidenceReturnBtn').click();

    await expect(page.locator('#toast')).toBeVisible();
    await expect(page.locator('#toast')).toContainText('请先导出当前固定范围的图表证据包');
    await expect(page.locator('#webAiReturnOverlay')).toBeHidden();

    // 2. 验证弹窗中的提示文案明确说明非自动任务且校验不写库
    const validationBox = page.locator('#webAiValidationResult');
    await expect(validationBox).toContainText('校验仅在当前页面内存中检查格式，不写数据库');
    await expect(validationBox).toContainText('应用结果不等于整篇论文已完成审核');

    // 3. 验证弹窗中不存在 webAiFinalizeEvidenceBtn 按钮
    await expect(page.locator('#webAiFinalizeEvidenceBtn')).toHaveCount(0);
  });

  test('审核中心界面文案不将科研核查责任推给普通用户', async ({ page }) => {
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/workbench/review-center') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            rows: [
              {
                paper_id: 'paper-text-check',
                paper_code: 'P9999',
                title: 'Text Check Paper',
                workflow_status: 'Needs_Human_Confirmation',
                needs_human_confirmation: true,
                dft_review_conflict_count: 2,
                has_dft_candidates: true,
                has_active_dft_candidates: true,
                pdf_storage_path: null,
                pdf_parse_status: 'none'
              }
            ],
            total: 1,
            stats: { A_text_readable: 0, B_text_partial: 0 }
          })
        });
      }
      if (url.pathname === '/api/libraries') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
      }
      if (url.pathname === '/api/system/agent-guide') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: '{"version":"test"}' });
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    // 1. 统计卡片文案：待确认改为“待 AI 核查”
    await expect(page.locator('#stats')).toContainText('待 AI 核查');
    await expect(page.locator('#stats')).not.toContainText('待确认');

    // 2. DFT 冲突提示文案客观说明
    const conflictStat = page.locator('#stats .stat').filter({ hasText: 'DFT 冲突' });
    await expect(conflictStat).toHaveAttribute('title', '此统计仅反映后端返回的 DFT 未决冲突，不代表图表或内容问题已经解决。');

    // 3. 真实 DOM 断言：工作流筛选器显示“待 AI 核查”，不显示“待确认”
    const workflowOption = page.locator('#workflowStatusFilter option[value="Needs_Human_Confirmation"]');
    await expect(workflowOption).toHaveText('待 AI 核查 (1)');
    await expect(page.locator('#workflowStatusFilter')).toContainText('待 AI 核查');
    await expect(page.locator('#workflowStatusFilter')).not.toContainText('待确认');

    // 4. 真实 DOM 断言：页面引导文案不包含“调整后再确认”或“仍需继续确认”
    await expect(page.locator('body')).not.toContainText('调整后再确认');
    await expect(page.locator('body')).not.toContainText('仍需继续确认');

    // 5. 源码文案核验：不将科研责任压给用户，包含 AI 继续核验文案
    const pageJsPath = path.resolve(__dirname, '../pages/review_center/page.js');
    const pageJsContent = fs.readFileSync(pageJsPath, 'utf8');
    expect(pageJsContent).toContain('Needs_Human_Confirmation: { label: "待 AI 核查"');
    expect(pageJsContent).toContain('Needs_Human_Confirmation: "待 AI 核查"');
    expect(pageJsContent).toContain('pending: "待核验"');
    expect(pageJsContent).toContain('已有待处理 DFT 候选，可复制任务交给 AI 核查，或查看对象详情');
    expect(pageJsContent).toContain('请将漏提核查任务交给 AI，由 AI 对照原文和表格检查；证据不足时报告 blocked');
    expect(pageJsContent).toContain('请复制任务交给 AI 核查；详情页仅用于查看真实对象与状态');
    expect(pageJsContent).toContain('缺少 PDF，需由 AI 或管理员补齐；无法取得时报告 blocked');
    expect(pageJsContent).toContain('AI 认为候选内容需要调整后由 AI 继续核验');
    expect(pageJsContent).toContain('候选状态仍需由 AI 继续核验');
    expect(pageJsContent).not.toContain('下一步去证据页核对字段和定位');
    expect(pageJsContent).not.toContain('确认它是否只保留元数据');
    expect(pageJsContent).not.toContain('确认是否还有漏掉的 DFT 行');
    expect(pageJsContent).not.toContain('调整后再确认');
    expect(pageJsContent).not.toContain('仍需继续确认');
  });

});
