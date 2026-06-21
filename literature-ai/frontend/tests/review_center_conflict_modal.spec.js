const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';

function jsonResponse(route, payload) {
  return route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

test.describe('Review Center Conflict Modal', () => {
  test('links grouped conflicts to read-only evidence preview', async ({ page }) => {
    const writeCalls = [];

    await page.route('**/api/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      const method = request.method();

      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: {
            returned: 2,
            quality_counts: { A_text_readable: 1, Broken: 1 },
          },
          rows: [
            {
              paper_id: 'paper-1',
              title: 'Stable Paper',
              year: 2025,
              journal: 'Journal of Testing',
              workflow_status: 'Needs_Human_Confirmation',
              pdf_quality_status: 'A_text_readable',
              pdf_exists: true,
              pdf_url: '/api/papers/paper-1/pdf',
              pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 1234, blocking_errors: [] },
              has_dft_candidates: true,
              dft_candidate_count: 1,
              dft_candidate_status_counts: { system_candidate: 1 },
              dft_audit: { status_label: 'Ready', detected_signal_count: 1, parsed_dft_count: 1, suspected_missing_count: 0 },
              dft_completeness_status: 'Initial_Parsed',
              dft_completeness_label: 'Initial_Parsed',
              suspected_missing_dft_count: 0,
              figure_count: 0,
              figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              figure_issue_count: 0,
              figure_issue_counts: {},
              top_figure_issues: [],
              table_count: 0,
              evidence_count: 1,
              locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              locator_issue_count: 0,
              locator_issue_counts: {},
              top_locator_issues: [],
              external_audit_count: 0,
              external_audit_opinions: [],
              object_review_audit_count: 0,
              object_review_audits: [],
              review_conflict_count: 0,
              workspace_path: '/workspace/paper-1',
              paper_short_id: 'stable01',
            },
            {
              paper_id: 'paper-2',
              title: 'Conflict Rich Paper',
              year: 2024,
              journal: 'Journal of Edge Cases',
              workflow_status: 'Unparsed',
              pdf_quality_status: 'A_text_readable',
              pdf_exists: true,
              pdf_url: '/api/papers/paper-2/pdf',
              pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 4567, blocking_errors: [] },
              has_dft_candidates: false,
              dft_candidate_count: 0,
              dft_candidate_status_counts: {},
              dft_audit: { status_label: 'Unparsed', detected_signal_count: 0, parsed_dft_count: 0, suspected_missing_count: 0 },
              dft_completeness_status: 'Unparsed',
              dft_completeness_label: 'Unparsed',
              suspected_missing_dft_count: 0,
              figure_count: 0,
              figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              figure_issue_count: 0,
              figure_issue_counts: {},
              top_figure_issues: [],
              table_count: 0,
              evidence_count: 0,
              locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              locator_issue_count: 0,
              locator_issue_counts: {},
              top_locator_issues: [],
              external_audit_count: 0,
              external_audit_opinions: [],
              object_review_audit_count: 0,
              object_review_audits: [],
              review_conflict_count: 2,
              workspace_path: '/workspace/paper-2',
              paper_short_id: 'cf73c0c5',
            },
          ],
        });
      }

      if (method === 'POST' && pathname === '/api/workbench/review-conflicts/accept-ai') {
        writeCalls.push({ method, pathname, body: request.postDataJSON() });
        return jsonResponse(route, {
          action: 'propose_correction',
          target_id: 'dft-paper-2-1',
          result: { status: 'pending', id: 'corr-1' },
        });
      }

      if (method === 'POST' && pathname === '/api/workbench/review-center/prepare-ai-materials') {
        writeCalls.push({ method, pathname, body: request.postDataJSON() });
        return jsonResponse(route, { completed: 1, failed: 0, requested: 1, rows: [] });
      }

      if (method === 'POST' && pathname === '/api/workbench/review-conflicts/auto-advance') {
        writeCalls.push({ method, pathname, body: request.postDataJSON() });
        return jsonResponse(route, {
          eligible: 1,
          executed: 1,
          skipped: 0,
          executed_items: [{ target_id: 'dft-paper-2-1', action: 'verify' }],
          skipped_items: [],
        });
      }

      if (method !== 'GET') {
        writeCalls.push({ method, pathname });
        return route.fulfill({ status: 204, body: '' });
      }

      if (pathname === '/api/workbench/review-conflicts' && url.searchParams.get('paper_id') === 'paper-2') {
        return jsonResponse(route, {
          adjudication_summary: {
            auto: 0,
            suggest: 1,
            manual: 1,
          },
          rows: [
            {
              target_type: 'dft_results',
              target_id: 'dft-paper-2-1',
              field_name: 'value',
              reviewer_count: 4,
              conflict_types: ['value_conflict', 'decision_conflict'],
              adjudication: {
                adjudication_mode: 'suggest',
                recommended_action: 'propose_correction',
                reason_summary: 'A stronger table-backed consensus favors a correction draft.',
                blocked_reasons: [],
                recommended_payload: { proposed_value: '-1.80' },
              },
              opinions: [
                {
                  source: 'assigned_dft_audit',
                  source_label: 'Gemini data audit',
                  model_name: 'gemini-test',
                  agent_role: 'data_auditor',
                  decision: 'accept',
                  confidence: 0.82,
                  value: '-1.80',
                  unit: 'eV',
                  reason: 'Matches Table 2.',
                  evidence: {
                    source_type: 'table',
                    source_label: 'Table 2',
                    evidence_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.80 eV in Table 2.',
                    context_before: 'Table 2 reports adsorption energies for representative active sites.',
                    context_after: 'The same table compares Fe-N4 against neighboring defect motifs.',
                    locator: { page: 5, locator_status: 'exact_page' },
                  },
                },
                {
                  source: 'external_analysis',
                  source_label: 'GLM review',
                  model_name: 'glm-test',
                  agent_role: 'cross_checker',
                  decision: 'revise',
                  confidence: 0.64,
                  value: '-1.75',
                  unit: 'eV',
                  reason: 'Caption and table disagree.',
                  evidence: {
                    source_type: 'section',
                    source_label: 'Section 3.2',
                    evidence_text: 'The discussion text mentions -1.75 eV before the authors summarize the preferred value.',
                    context_before: 'Section 3.2 explains why the caption and the table can diverge.',
                    context_after: 'A follow-up paragraph notes the tabulated value should be reviewed manually.',
                    locator: { page: 5, locator_status: 'text_only' },
                  },
                },
                {
                  source: 'manual_review',
                  source_label: 'Human reviewer',
                  model_name: 'human',
                  agent_role: 'human_reviewer',
                  decision: 'accept',
                  confidence: 0.71,
                  value: '-1.80',
                  unit: 'eV',
                  reason: 'Manual check still supports the tabulated value.',
                  evidence: {
                    source_type: 'table',
                    source_label: 'Table 2',
                    evidence_text: 'A manual read still supports -1.80 eV for the selected row.',
                    locator: { page: 5, locator_status: 'exact_page' },
                  },
                },
                {
                  source: 'cross_lab',
                  source_label: 'Claude lab review',
                  model_name: 'claude-test',
                  agent_role: 'cross_checker',
                  decision: 'review',
                  confidence: 0.43,
                  value: '-1.78',
                  unit: 'eV',
                  reason: 'This rationale is intentionally long so the modal must clamp it by default and then allow the reviewer to expand the full explanation without losing table alignment across rows.',
                  evidence: {
                    source_type: 'section',
                    source_label: 'Discussion',
                    evidence_text: 'The lab summary mentions a likely discrepancy but does not keep the page anchor.',
                    locator: { locator_status: 'missing_page' },
                  },
                },
              ],
            },
            {
              target_type: 'writing_card',
              target_id: 'writing-card-2',
              field_name: 'core_hypothesis',
              reviewer_count: 2,
              conflict_types: ['mapping_conflict'],
              adjudication: {
                adjudication_mode: 'manual',
                recommended_action: 'jump_to_review',
                reason_summary: 'Writing-card conflicts should stay in object review.',
                blocked_reasons: ['requires_object_review'],
              },
              opinions: [
                {
                  source: 'assigned_writing_audit',
                  source_label: 'Claude writing audit',
                  model_name: 'claude-test',
                  agent_role: 'writing_auditor',
                  decision: 'review',
                  confidence: 0.58,
                  reason: 'Hypothesis may map to mechanism claim instead.',
                  evidence: {
                    source_type: 'writing_card',
                    source_label: 'Writing card',
                    evidence_text: 'Defect sites alter adsorption and charge redistribution, which may fit the mechanism claim better.',
                    context_before: 'The writing card summarizes the study framing for the introduction.',
                    locator: { page: 2, locator_status: 'exact_page' },
                  },
                },
                {
                  source: 'external_analysis',
                  source_label: 'Gemini writing review',
                  model_name: 'gemini-test',
                  agent_role: 'writing_checker',
                  decision: 'review',
                  confidence: 0.61,
                  reason: 'Current mapping still looks valid.',
                  evidence: {
                    source_type: 'section',
                    source_label: 'Section 2.1',
                    evidence_text: 'The authors explicitly connect the hypothesis to the writing card summary in Section 2.1.',
                    context_after: 'A nearby sentence reinforces the intended narrative thread.',
                    locator: { page: 2, locator_status: 'exact_page' },
                  },
                },
              ],
            },
          ],
        });
      }

      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    const rows = page.locator('#rows tr');
    await expect(rows).toHaveCount(2);

    await page.locator('[data-action="open-conflicts"]').first().click();

    const overlay = page.locator('#infoOverlay.open');
    await expect(overlay).toBeVisible();
    await expect(overlay).toContainText('冲突详情');
    await expect(overlay).toContainText('只读聚合，不自动合并');
    await expect(overlay).toContainText('当前还有 1 个需要关注的冲突');
    await expect(overlay).toContainText('冲突列表');
    await expect(overlay).toContainText('证据预览');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('The adsorption energy of Li2S4 on Fe-N4 is -1.80 eV in Table 2.');
    await expect(overlay).toContainText('自动推进 0');
    await expect(overlay).toContainText('建议裁定 1');
    await expect(overlay).toContainText('必须人工 1');
    await expect(overlay).toContainText('接受 AI 裁定');
    await expect(overlay).toContainText('生成修正草案');
    await expect(overlay).toContainText('跳到对象审核');

    const conflictItems = overlay.locator('.conflict-list-item');
    await expect(conflictItems).toHaveCount(2);
    await expect(conflictItems.nth(0)).toHaveClass(/is-active/);
    const selectedPanel = overlay.locator('#selectedConflictPanel');
    await expect(selectedPanel).toContainText('dft-paper-2-1');
    await conflictItems.nth(1).click();
    await expect(conflictItems.nth(1)).toHaveClass(/is-active/);
    await expect(selectedPanel).toContainText('Writing-card conflicts should stay in object review.');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('Defect sites alter adsorption and charge redistribution');
    await expect(selectedPanel).not.toContainText('接受 AI 裁定');
    await conflictItems.nth(0).click();
    await expect(conflictItems.nth(0)).toHaveClass(/is-active/);
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('The adsorption energy of Li2S4 on Fe-N4 is -1.80 eV in Table 2.');
    await overlay.getByRole('button', { name: '隐藏冲突列表' }).click();
    await expect(overlay.locator('.conflict-list-panel')).toBeHidden();
    await expect(overlay.getByRole('button', { name: '显示冲突列表' })).toBeVisible();
    await expect.poll(async () => {
      return overlay.locator('.compare-table-wrap').evaluate(node => node.scrollWidth <= node.clientWidth + 1);
    }).toBe(true);

    const firstTableRows = selectedPanel.locator('tbody tr');
    await expect(firstTableRows).toHaveCount(4);
    await expect(firstTableRows.filter({ hasText: 'Claude lab review' })).toBeHidden();

    await overlay.getByRole('button', { name: '展开全部意见（+1）' }).click();
    await expect(firstTableRows.filter({ hasText: 'Claude lab review' })).toBeVisible();

    const viewEvidenceButtons = overlay.getByRole('button', { name: '查看原文' });
    await expect(viewEvidenceButtons.first()).toBeVisible();

    await viewEvidenceButtons.nth(0).click();
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('来源对象');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('页码');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('定位');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('原文片段');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('Table 2');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('The adsorption energy of Li2S4 on Fe-N4 is -1.80 eV in Table 2.');
    await expect(firstTableRows.nth(0)).toHaveClass(/is-active/);

    await viewEvidenceButtons.nth(1).click();
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('Section 3.2');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('The discussion text mentions -1.75 eV before the authors summarize the preferred value.');
    await expect(overlay.locator('#conflictEvidencePanel')).toContainText('text_only');
    await expect(firstTableRows.nth(1)).toHaveClass(/is-active/);

    await expect(overlay.getByRole('link', { name: 'Open page 5' }).first()).toBeVisible();
    await expect(overlay.locator('button[disabled][title="当前定位不足，不能可靠跳页"]')).toHaveCount(2);

    const expandReason = overlay.getByRole('button', { name: '展开理由' }).first();
    await expandReason.click();
    await expect(overlay.getByRole('button', { name: '收起理由' }).first()).toBeVisible();

    await overlay.getByRole('button', { name: '接受 AI 裁定' }).click();
    await expect(page.locator('#toast')).toContainText('AI 裁定已执行');
    expect(writeCalls).toContainEqual({
      method: 'POST',
      pathname: '/api/workbench/review-conflicts/accept-ai',
      body: {
        paper_id: 'paper-2',
        target_type: 'dft_results',
        target_id: 'dft-paper-2-1',
        field_name: 'value',
        reviewer: 'review_center',
      },
    });

    const jumpLink = overlay.getByRole('link', { name: '跳到对象审核' }).first();
    const jumpHref = await jumpLink.getAttribute('href');
    const jumpUrl = new URL(jumpHref, `${BASE_URL}/pages/review_center/index.html`);
    expect(jumpUrl.pathname).toBe('/pages/literature_library/index.html');
    expect(jumpUrl.searchParams.get('paper_id')).toBe('paper-2');
    expect(jumpUrl.searchParams.get('tab')).toBe('dft');
    expect(jumpUrl.searchParams.get('target_type')).toBe('dft_results');
    expect(jumpUrl.searchParams.get('target_id')).toBe('dft-paper-2-1');
    expect(jumpUrl.searchParams.get('field_name')).toBe('value');
    expect(jumpUrl.searchParams.get('pdf_page')).toBe('5');
    expect(jumpUrl.searchParams.get('pdf_locator_status')).toBe('exact_page');
    expect(jumpUrl.searchParams.get('pdf_evidence_text')).toContain('The adsorption energy of Li2S4 on Fe-N4 is -1.80 eV in Table 2.');

    await overlay.getByRole('button', { name: '关闭' }).first().click();
    await expect(overlay).toBeHidden();
    await expect(page.getByRole('button', { name: '批量 AI 自动推进当前筛选' })).toHaveCount(0);
  });

  test('status filter includes real workflow statuses and matches Parsed_Material_Ready exactly', async ({ page }) => {
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: {
            returned: 3,
            quality_counts: { A_text_readable: 3 },
          },
          rows: [
            {
              paper_id: 'paper-ready',
              title: 'Prepared Materials Paper',
              year: 2025,
              journal: 'Journal A',
              workflow_status: 'Parsed_Material_Ready',
              pdf_quality_status: 'A_text_readable',
              pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 100, blocking_errors: [] },
              has_dft_candidates: false,
              dft_candidate_count: 0,
              dft_candidate_status_counts: {},
              dft_audit: { status_label: 'Ready', detected_signal_count: 0, parsed_dft_count: 0, suspected_missing_count: 0 },
              dft_completeness_status: 'Initial_Parsed',
              dft_completeness_label: 'Initial_Parsed',
              suspected_missing_dft_count: 0,
              figure_count: 0,
              figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              figure_issue_count: 0,
              figure_issue_counts: {},
              top_figure_issues: [],
              table_count: 0,
              evidence_count: 0,
              locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              locator_issue_count: 0,
              locator_issue_counts: {},
              top_locator_issues: [],
              external_audit_count: 0,
              external_audit_opinions: [],
              object_review_audit_count: 0,
              object_review_audits: [],
              review_conflict_count: 0,
            },
            {
              paper_id: 'paper-human',
              title: 'Needs Human Paper',
              year: 2024,
              journal: 'Journal B',
              workflow_status: 'Needs_Human_Confirmation',
              needs_human_confirmation: true,
              pdf_quality_status: 'A_text_readable',
              pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 101, blocking_errors: [] },
              has_dft_candidates: true,
              dft_candidate_count: 1,
              dft_candidate_status_counts: { system_candidate: 1 },
              dft_audit: { status_label: 'Ready', detected_signal_count: 1, parsed_dft_count: 1, suspected_missing_count: 0 },
              dft_completeness_status: 'Initial_Parsed',
              dft_completeness_label: 'Initial_Parsed',
              suspected_missing_dft_count: 0,
              figure_count: 0,
              figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              figure_issue_count: 0,
              figure_issue_counts: {},
              top_figure_issues: [],
              table_count: 0,
              evidence_count: 0,
              locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              locator_issue_count: 0,
              locator_issue_counts: {},
              top_locator_issues: [],
              external_audit_count: 0,
              external_audit_opinions: [],
              object_review_audit_count: 0,
              object_review_audits: [],
              review_conflict_count: 0,
            },
            {
              paper_id: 'paper-ready-ml',
              title: 'Ready ML Paper',
              year: 2023,
              journal: 'Journal C',
              workflow_status: 'ML_Ready',
              pdf_quality_status: 'A_text_readable',
              pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 102, blocking_errors: [] },
              has_dft_candidates: true,
              dft_candidate_count: 1,
              dft_candidate_status_counts: { ML_Ready: 1 },
              dft_audit: { status_label: 'Ready', detected_signal_count: 1, parsed_dft_count: 1, suspected_missing_count: 0 },
              dft_completeness_status: 'DB_Ready',
              dft_completeness_label: 'DB_Ready',
              suspected_missing_dft_count: 0,
              figure_count: 0,
              figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              figure_issue_count: 0,
              figure_issue_counts: {},
              top_figure_issues: [],
              table_count: 0,
              evidence_count: 0,
              locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              locator_issue_count: 0,
              locator_issue_counts: {},
              top_locator_issues: [],
              external_audit_count: 0,
              external_audit_opinions: [],
              object_review_audit_count: 0,
              object_review_audits: [],
              review_conflict_count: 0,
            },
          ],
        });
      }

      if (pathname === '/api/libraries') {
        return jsonResponse(route, []);
      }

      if (pathname === '/api/system/agent-guide') {
        return jsonResponse(route, {
          version: 'test',
          review_prompts: { templates: {}, composite_templates: {} },
        });
      }

      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, { rows: [], adjudication_summary: { auto: 0, suggest: 0, manual: 0 } });
      }

      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    await expect(page.locator('#statusFilter option[value="group:ai_processing"]')).toContainText('AI 处理中 (1)');
    await expect(page.locator('#workflowStatusFilter option[value="Parsed_Material_Ready"]')).toContainText('材料已就绪 (1)');
    await expect(page.locator('#workflowStatusFilter option[value="Needs_Human_Confirmation"]')).toContainText('待人工确认 (1)');
    await expect(page.locator('#statusFilter option[value="Parsed_Material_Ready"]')).toHaveCount(0);

    await page.selectOption('#workflowStatusFilter', 'Parsed_Material_Ready');
    await expect(page.locator('#queueMeta')).toContainText('当前筛选 1 篇');
    await expect(page.locator('#rows tr')).toHaveCount(1);
    await expect(page.locator('#rows')).toContainText('Prepared Materials Paper');
    await expect(page.locator('#rows')).not.toContainText('Needs Human Paper');
    await expect(page.locator('#rows')).not.toContainText('Ready ML Paper');
  });

  test('keeps review center filter state during the current session', async ({ page }) => {
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: {
            returned: 2,
            quality_counts: { A_text_readable: 1, B_text_partial: 1 },
          },
          rows: [
            {
              paper_id: 'paper-session-1',
              title: 'Session Filter Paper A',
              year: 2025,
              journal: 'Session Journal',
              library_name: 'Default Library',
              workflow_status: 'Parsed_Material_Ready',
              pdf_quality_status: 'A_text_readable',
              pdf_exists: true,
              pdf_url: '/api/papers/paper-session-1/pdf',
              review_conflict_count: 0,
              review_conflict_total_count: 0,
            },
            {
              paper_id: 'paper-session-2',
              title: 'Session Filter Paper B',
              year: 2024,
              journal: 'Another Journal',
              library_name: 'Archive Library',
              workflow_status: 'Needs_Human_Confirmation',
              pdf_quality_status: 'B_text_partial',
              pdf_exists: true,
              pdf_url: '/api/papers/paper-session-2/pdf',
              review_conflict_count: 1,
              review_conflict_total_count: 1,
            },
          ],
        });
      }

      if (pathname === '/api/libraries') {
        return jsonResponse(route, [
          { name: 'Default Library', is_active: true },
          { name: 'Archive Library', is_active: false },
        ]);
      }

      if (pathname === '/api/system/agent-guide') {
        return jsonResponse(route, {
          version: 'test',
          review_prompts: { templates: {}, composite_templates: {} },
        });
      }

      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, { rows: [], adjudication_summary: { auto: 0, suggest: 0, manual: 0 } });
      }

      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.evaluate(() => window.sessionStorage.removeItem('litai:review-center:filters:v1'));
    await page.reload();

    await page.selectOption('#libraryFilter', 'Archive Library');
    await page.selectOption('#statusFilter', 'group:needs_action');
    await page.selectOption('#workflowStatusFilter', 'Needs_Human_Confirmation');
    await page.selectOption('#qualityFilter', 'B_text_partial');
    await page.selectOption('#sortFilter', 'year_desc');
    await page.fill('#searchBox', 'Session Filter Paper B');
    await page.reload();

    await expect(page.locator('#libraryFilter')).toHaveValue('Archive Library');
    await expect(page.locator('#statusFilter')).toHaveValue('group:needs_action');
    await expect(page.locator('#workflowStatusFilter')).toHaveValue('Needs_Human_Confirmation');
    await expect(page.locator('#qualityFilter')).toHaveValue('B_text_partial');
    await expect(page.locator('#sortFilter')).toHaveValue('year_desc');
    await expect(page.locator('#searchBox')).toHaveValue('Session Filter Paper B');
  });

  test('shows resolved conflict history as a subtle badge instead of an active red conflict badge', async ({ page }) => {
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/workbench/review-center') {
        return jsonResponse(route, {
          metadata: {
            returned: 1,
            quality_counts: { A_text_readable: 1 },
          },
          rows: [
            {
              paper_id: 'paper-resolved',
              title: 'Resolved Conflict Paper',
              year: 2025,
              journal: 'Journal D',
              workflow_status: 'Parsed_Material_Ready',
              pdf_quality_status: 'A_text_readable',
              pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 120, blocking_errors: [] },
              has_dft_candidates: true,
              has_active_dft_candidates: false,
              active_dft_candidate_count: 0,
              dft_candidate_count: 1,
              dft_candidate_status_counts: { ML_Ready: 1 },
              dft_audit: { status_label: 'Ready', detected_signal_count: 1, parsed_dft_count: 1, suspected_missing_count: 0 },
              dft_completeness_status: 'DB_Ready',
              dft_completeness_label: 'DB_Ready',
              suspected_missing_dft_count: 0,
              figure_count: 0,
              figure_crop_status_counts: {},
              figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              figure_issue_count: 0,
              figure_issue_counts: {},
              top_figure_issues: [],
              table_count: 0,
              evidence_count: 1,
              locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
              locator_issue_count: 0,
              locator_issue_counts: {},
              top_locator_issues: [],
              external_audit_count: 0,
              external_audit_opinions: [],
              object_review_audit_count: 0,
              object_review_audits: [],
              paper_note_count: 0,
              latest_paper_notes: [],
              review_conflict_count: 0,
              review_conflict_total_count: 2,
            },
          ],
        });
      }

      if (pathname === '/api/libraries') {
        return jsonResponse(route, []);
      }

      if (pathname === '/api/system/agent-guide') {
        return jsonResponse(route, {
          version: 'test',
          review_prompts: { templates: {}, composite_templates: {} },
        });
      }

      if (pathname === '/api/workbench/review-conflicts') {
        return jsonResponse(route, { rows: [], adjudication_summary: { auto: 0, suggest: 0, manual: 0 } });
      }

      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);

    await expect(page.locator('#stats .stat').filter({ hasText: '未收口冲突' })).toContainText('0');
    await expect(page.locator('#rows .chip.subtle[title*="历史上出现过冲突"]')).toContainText('已处理冲突 2');
    await expect(page.locator('#rows [data-action="open-conflicts"]')).toHaveCount(0);
  });
});
