with open('index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update thead
old_thead = """          <thead>
            <tr>
              <th>文献</th>
              <th>流程状态</th>
              <th>PDF 质量</th>
              <th>证据</th>
              <th>DFT</th>
              <th>工作目录</th>
              <th>操作</th>
            </tr>
          </thead>"""
new_thead = """          <thead>
            <tr>
              <th>文献信息</th>
              <th>综合状态</th>
              <th>提取数据</th>
              <th>操作</th>
            </tr>
          </thead>"""
content = content.replace(old_thead, new_thead)

# 2. Update colspan 7 -> 4
content = content.replace('<td colspan="7">', '<td colspan="4">')

# 3. Update return '<tr>' block
old_return = """        const candidateCell = row.has_dft_candidates
          ? '<span class="chip warn" title="已检测到可进一步审核的 DFT 字段候选。">待审 DFT 候选</span><div class="muted">已抽出待核对的 DFT 字段</div>'
          : '<span class="chip" title="当前轮次没有抽出可核对的 DFT 字段。">无 DFT 候选</span><div class="muted">当前轮次未抽出可核对的 DFT 字段</div>';
        return '<tr>' +
          '<td><div class="paper-title">' + esc(row.title || "未命名文献") + '</div><div class="muted">' + formatBibMeta(row.year, row.journal) + '</div></td>' +
          '<td><span class="chip ' + workflowClass(row.workflow_status) + '" title="' + esc(workflow.tip + " 原始状态码: " + (row.workflow_status || "Imported")) + '">' + esc(workflow.label) + '</span><div class="muted">悬停可看说明</div></td>' +
          '<td><span class="chip ' + qualityClass(row.pdf_quality_status) + '" title="' + esc(quality.tip + " 原始质量码: " + (row.pdf_quality_status || "unknown")) + '">' + esc(quality.label) + '</span><div class="muted">质量分: ' + esc(row.pdf_quality_score == null ? "-" : row.pdf_quality_score) + '</div><div class="muted" title="' + esc("原始原因码: " + (row.quality_reason || "")) + '">' + esc(qualityReasonText) + '</div></td>' +
          '<td><div class="muted" title="证据定位条数，表示当前已经记录了多少条可回溯的正文、图表或表格定位。">证据定位: ' + esc(evidence) + ' 条</div><div class="muted" title="当前已索引的图片和表格数量。">图片: ' + esc(figureCount) + ' 张 / 表格: ' + esc(tableCount) + ' 个</div></td>' +
          '<td>' + candidateCell + '</td>' +
          '<td><div class="muted mono">' + esc(workspace) + '</div></td>' +
          '<td><div class="actions">' +
            '<a class="btn ghost small" href="' + esc(detailUrl) + '">' + esc(inspectTarget.label) + '</a>' +
            '<button class="btn ghost small" type="button" onclick="preparePaper(\\'\\' + esc(row.paper_id) + '\\')">重建材料</button>' +
            '<button class="btn primary small" type="button" onclick="humanConfirm(\\'\\' + esc(row.paper_id) + '\\')">人工确认</button>' +
            '<div class="action-note">' + esc(inspectTarget.note) + '</div>' +
          '</div></td>' +
        '</tr>';"""

new_return = """        const dftChip = row.has_dft_candidates
          ? '<span class="chip warn" title="待审 DFT 候选：已检测到可进一步审核的 DFT 字段候选。">待审 DFT 候选</span>'
          : '<span class="chip" title="无 DFT 候选：当前轮次未抽出可核对的 DFT 字段。">无 DFT 候选</span>';
        const wfChip = '<span class="chip ' + workflowClass(row.workflow_status) + '" title="' + esc(workflow.tip + " 原始状态码: " + (row.workflow_status || "Imported")) + '">' + esc(workflow.label) + '</span>';
        const qlChip = '<span class="chip ' + qualityClass(row.pdf_quality_status) + '" title="质量分: ' + esc(row.pdf_quality_score == null ? "-" : row.pdf_quality_score) + '。原因: ' + esc(qualityReasonText) + '。' + esc(quality.tip) + '">' + esc(quality.label) + '</span>';
        
        let wsDisp = workspace;
        if (wsDisp.length > 8) { wsDisp = "... " + wsDisp.slice(-8); }

        return '<tr>' +
          '<td><div class="paper-title" title="' + esc(row.title || "未命名文献") + '">' + esc(row.title || "未命名文献") + '</div><div class="muted">' + formatBibMeta(row.year, row.journal) + ' &nbsp;|&nbsp; <span class="mono" style="font-size: 11px; opacity: 0.6;" title="工作目录 ID：' + esc(workspace) + '">' + esc(wsDisp) + '</span></div></td>' +
          '<td><div style="display: flex; gap: 6px; flex-wrap: wrap;">' + wfChip + qlChip + dftChip + '</div></td>' +
          '<td><div class="muted">定位: <strong>' + esc(evidence) + '</strong> &nbsp;·&nbsp; 图表: <strong>' + esc(figureCount + tableCount) + '</strong></div></td>' +
          '<td><div class="actions">' +
            '<a class="btn btn-ghost btn-sm" href="' + esc(detailUrl) + '" title="' + esc(inspectTarget.note) + '">' + esc(inspectTarget.label) + '</a>' +
            '<button class="btn btn-ghost btn-sm" type="button" onclick="preparePaper(\\'\\' + esc(row.paper_id) + '\\')">重建材料</button>' +
            '<button class="btn btn-tinted btn-sm" type="button" onclick="humanConfirm(\\'\\' + esc(row.paper_id) + '\\')">人工确认</button>' +
          '</div></td>' +
        '</tr>';"""

content = content.replace(old_return, new_return)

with open('index.html', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated index.html successfully via precise text replacement.")
