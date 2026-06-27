// Paper detail page composition.
function renderDetail(detail, audit) {
    const counts = detail.counts || {};
    const summaryCards =
        '<div class="cards">' +
            '<div class="stat-card"><h3>章节</h3><div class="value">' + (counts.sections || 0) + "</div></div>" +
            '<div class="stat-card"><h3>表格</h3><div class="value">' + (counts.tables || 0) + "</div></div>" +
            '<div class="stat-card"><h3>图片</h3><div class="value">' + (counts.figures || 0) + "</div></div>" +
            '<div class="stat-card"><h3>DFT 候选</h3><div class="value">' + (counts.dft_results || 0) + "</div></div>" +
            '<div class="stat-card"><h3>机理</h3><div class="value">' + (counts.mechanism_claims || 0) + "</div></div>" +
            '<div class="stat-card"><h3>写作卡</h3><div class="value">' + (counts.writing_cards || 0) + "</div></div>" +
        "</div>";

    const baseInfo =
        '<details class="section-card"><summary><h3>基础信息</h3></summary>' +
            '<div class="inline-grid">' +
                '<div class="key-value"><div class="k">文献库</div><div class="v">' + esc(detail.library_name || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">文献类型</div><div class="v">' + esc(paperTypeLabel(detail.paper_type)) + (detail.type_confidence ? ' (置信度 ' + detail.type_confidence + ')' : '') + '</div></div>' +
                '<div class="key-value"><div class="k">分类来源</div><div class="v">' + esc(detail.classification_source || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">创建时间</div><div class="v">' + esc(formatDate(detail.created_at)) + '</div></div>' +
                '<div class="key-value"><div class="k">PDF 路径</div><div class="v">' + esc(detail.pdf_path || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">Markdown 路径</div><div class="v">' + esc(detail.markdown_path || "-") + '</div></div>' +
            "</div>" +
        "</details>";

    const abstractReviewStatus = contentReviewStatus(detail, "abstract_review_status");
    const sectionsReviewStatus = contentReviewStatus(detail, "sections_review_status");
    const writingCardsReviewStatus = contentReviewStatus(detail, "writing_cards_review_status");
    const translationReviewStatus = contentReviewStatus(detail, "translation_review_status");
    const abstractCard = isAiVerifiedStatus(abstractReviewStatus)
        ? '<details class="section-card"><summary><h3>摘要</h3></summary><div class="prewrap">' + esc(detail.abstract || "暂无摘要。") + "</div></details>"
        : renderPendingReviewCard("摘要", "摘要待 AI 核验，不在详情页展示。");

    const localizedSummaryCard = renderLocalizedSummary(detail);
    const comprehensiveCard = "";

    const activeTab = state.currentTab || "summary";
    let sectionCards = "";
    if (activeTab === "sections") {
        if (isAiVerifiedStatus(sectionsReviewStatus)) {
            const displaySections = (detail.sections || []).filter(isDisplayBodySection).sort(compareDisplaySections).slice(0, 8);
            if (displaySections.length) {
            sectionCards = renderListBlock("章节内容", displaySections, function(item) {
                const text = cleanPdfExtractedText(item.text || "");
                return '<div class="prewrap">' + esc(ellipsis(text, 2200) || "暂无文本。") + "</div>";
            }, function(item) {
                const title = cleanPdfExtractedText(item.section_title || item.section_type || "未命名章节");
                return esc(title);
            });
            } else {
                sectionCards = renderPendingReviewCard("\u7ae0\u8282", "\u5f53\u524d\u53ea\u5269\u6574\u9875\u5207\u5206\u6216\u5df2\u5e9f\u5f03\u7ae0\u8282\uff0c\u6682\u65f6\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u7ed3\u6784\u5316\u7ae0\u8282\u3002");
            }
        } else {
            sectionCards = renderPendingReviewCard("章节", "章节待 AI 核验。");
        }
    }

    let figureCards = "";
    if (activeTab === "figures" && detail.figures && detail.figures.length) {
        function extractFigureNumber(caption) {
            if (!caption) return null;
            var m = String(caption).match(/(?:Figure|Fig\.?|Scheme)\s*\.?\s*(\d+)(?:\s*[\(\[]?\s*([a-z])\s*[\)\]]?)?/i);
            if (!m) return null;
            var num = parseInt(m[1], 10);
            return Number.isFinite(num) ? num : null;
        }

        function extractFigureSortParts(text) {
            if (!text) return { number: null, subRank: null };
            var m = String(text).match(/(?:Figure|Fig\.?|Scheme)\s*\.?\s*(\d+)(?:\s*[\(\[]?\s*([a-z])\s*[\)\]]?)?/i);
            if (!m) return { number: null, subRank: null };
            var num = parseInt(m[1], 10);
            if (!Number.isFinite(num)) return { number: null, subRank: null };
            var sub = m[2] ? m[2].toLowerCase().charCodeAt(0) - 96 : null;
            return {
                number: num,
                subRank: Number.isFinite(sub) ? sub : null
            };
        }

        function figureSortNumber(item) {
            return extractFigureNumber(item.figure_label) || extractFigureNumber(item.caption);
        }

        function figureSortParts(item) {
            var labelParts = extractFigureSortParts(item.figure_label);
            if (labelParts.number !== null) return labelParts;
            return extractFigureSortParts(item.caption);
        }

        function compareFigureItems(a, b) {
            var aPage = Number(a.page || 999999);
            var bPage = Number(b.page || 999999);
            if (aPage !== bPage) return aPage - bPage;
            var aParts = figureSortParts(a);
            var bParts = figureSortParts(b);
            if (aParts.number !== null && bParts.number !== null && aParts.number !== bParts.number) return aParts.number - bParts.number;
            if (aParts.number !== null && bParts.number === null) return -1;
            if (aParts.number === null && bParts.number !== null) return 1;
            if (aParts.subRank !== null && bParts.subRank !== null && aParts.subRank !== bParts.subRank) return aParts.subRank - bParts.subRank;
            if (aParts.subRank !== null && bParts.subRank === null) return -1;
            if (aParts.subRank === null && bParts.subRank !== null) return 1;
            return String(a.id || "").localeCompare(String(b.id || ""));
        }

        function figureRoleLabel(role) {
            var raw = String(role || "").trim();
            var key = raw.toLowerCase();
            if (!key || key === "unknown") return "未分类/待核对";
            var labels = {
                plot: "数据图",
                data_plot: "数据图",
                structure: "结构图",
                schematic: "示意图",
                method: "方法图",
                microscopy: "显微图",
                spectra: "谱图",
                table_snapshot: "表格截图"
            };
            return labels[key] || raw;
        }

        const sortedFigures = detail.figures.slice().sort(compareFigureItems);
        const figureBlockedMap = figureRagBlockedMap(detail);
        const roles = new Set();
        sortedFigures.forEach(f => {
            if (f.figure_role) roles.add(f.figure_role);
        });

        let filterHtml = '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">' +
            '<button class="btn primary small" type="button" onclick="recropPaperFigures(\'' + escAttr(detail.id) + '\')">重新定位/重裁图</button>' +
            '<span class="status-chip meta" title="裁剪图只作为候选图，必须对照 PDF 原页、图注和上下文。">图片定位可靠性需审核</span>' +
        '</div>';
        if (roles.size > 0) {
            filterHtml += '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">';
            filterHtml += '<button class="btn small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display=\'block\')">全部</button>';
            filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.ragBlocked === \'true\' ? \'block\' : \'none\')">只看 RAG 不合格</button>';
            filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.ragBlocked === \'false\' ? \'block\' : \'none\')">只看 RAG 可用</button>';
            roles.forEach(role => {
                filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.role === \'' + esc(role) + '\' ? \'block\' : \'none\')">' + esc(figureRoleLabel(role)) + '</button>';
            });
            filterHtml += '</div>';
        }

        const noisyCount = sortedFigures.filter(isLikelyNoisyFigure).length;
        const cardsHtml = sortedFigures.map(function(item, index) {
            const ragBlocked = figureBlockedMap[String(item.id || "")] || null;
            const ragReasons = ragBlocked && Array.isArray(ragBlocked.reasons) ? ragBlocked.reasons : [];
            const ragStatusHtml = ragBlocked
                ? '<span class="status-chip danger" title="' + escAttr(ragReasons.map(ragReasonLabel).join("；")) + '">RAG 不合格：' + esc(ragReasons.map(ragReasonLabel).join("；") || "未通过") + '</span>'
                : '<span class="status-chip ok">RAG 可用</span>';
            let imgHtml = "";
            const noisyFigure = isLikelyNoisyFigure(item);
            if (item.image_path && !noisyFigure) {
                const imageSrc = item.asset_url || ("/api/papers/assets/" + item.image_path);
                const lightboxPayload = escAttr(JSON.stringify({
                    src: imageSrc,
                    title: item.figure_label || ("图片 " + (index + 1)),
                    caption: item.caption || "",
                    page: item.page || "",
                    alt: item.figure_label || ("图片 " + (index + 1))
                }));
                const openPageAction = item.page
                    ? buildPdfJumpButtonHtml({
                        paperId: detail.id,
                        page: item.page,
                        evidenceText: clipText(item.caption || "", 160),
                        label: "打开图片对应页面",
                        stopPropagation: true
                    })
                    : "";
                imgHtml = '<figure class="figure-image-block">' +
                    '<button type="button" class="figure-lightbox-trigger" onclick="event.stopPropagation(); openFigureLightbox(' + lightboxPayload + ')" style="display:block;width:100%;padding:0;border:0;background:transparent;cursor:zoom-in;">' +
                        '<img src="' + escAttr(imageSrc) + '" loading="lazy" decoding="async" alt="Parsed paper figure" style="display:block;width:100%;max-height:420px;object-fit:contain;border:1px solid var(--color-border);border-radius:var(--radius-sm);background:var(--color-surface);" />' +
                    '</button>' +
                    '<figcaption><span class="figure-image-actions"><button type="button" class="btn ghost small" onclick="event.stopPropagation(); openFigureLightbox(' + lightboxPayload + ')">点击查看大图</button>' + openPageAction + '</span><span class="subtle figure-image-label">' + esc(item.figure_label || ("figure " + (index + 1))) + '</span></figcaption>' +
                '</figure>';
            } else if (!item.image_path) {
                var missingPageLink = item.page
                    ? '<a class="btn ghost small" style="text-decoration:none;" target="_blank" href="/api/papers/' + escAttr(detail.id) + '/pdf#page=' + Number(item.page || 1) + '&toolbar=0">打开 PDF 原页</a>'
                    : "";
                imgHtml = '<div class="figure-warning figure-missing-image">' +
                    '<strong>当前没有可展示的裁图。</strong>' +
                    '<div>这张图的结构化记录已保留，但 image_path 为空，页面只能显示文字信息和证据入口。后续补图后会自动恢复为图片卡片。</div>' +
                    (missingPageLink ? '<div style="margin-top:8px;">' + missingPageLink + '</div>' : '') +
                '</div>';
            } else if (noisyFigure) {
                var pageLink = item.page
                    ? '<a class="btn ghost small" style="text-decoration:none;" target="_blank" href="/api/papers/' + escAttr(detail.id) + '/pdf#page=' + Number(item.page || 1) + '&toolbar=0">打开 PDF 原页</a>'
                    : "";
                imgHtml = '<div class="figure-warning">' +
                    '<strong>自动截图疑似不是有效论文图。</strong>' +
                    '<div>这类图片通常是页眉、出版社标志或 CrossMark，不会作为可靠图表使用。请以 PDF 原页、图注和正文证据为准。</div>' +
                    (pageLink ? '<div style="margin-top:8px;">' + pageLink + '</div>' : '') +
                '</div>';
            }

            let metaHtml = "";
            if (item.figure_role) {
                metaHtml += '<span class="status-chip" style="background: var(--color-primary-bg); color: var(--color-text-secondary); margin-right: 8px;">' + esc(figureRoleLabel(item.figure_role)) + (item.role_confidence ? ' (' + (item.role_confidence*100).toFixed(0) + '%)' : '') + '</span>';
            }
            if (item.key_elements && item.key_elements.length) {
                item.key_elements.forEach(el => {
                    metaHtml += '<span class="status-chip meta" style="margin-right: 4px;">' + esc(figureTermLabel(el)) + '</span>';
                });
            }
            if (metaHtml) {
                metaHtml = '<div style="margin-top: 8px;">' + metaHtml + '</div>';
            }

            let summaryHtml = "";
            if (item.content_summary) {
                summaryHtml = '<div class="subtle" style="margin-top: 8px; font-weight: 500;">' + esc(item.content_summary) + '</div>';
            }

            var figNum = figureSortNumber(item);
            var figLabel = figNum !== null ? '图片 ' + figNum : '图片 ' + (index + 1);
            var codexAction = codexItemActionHtml("figure", item);
            var directDeleteAction = item.direct_delete_eligible
                ? '<button type="button" class="btn danger small" onclick="event.stopPropagation(); directDeleteFigure(\'' + escAttr(detail.id) + '\', \'' + escAttr(item.id) + '\', this)">直接删除</button>'
                : '';
            var legacyDeleteNote = Number(item.pending_delete_proposal_count || 0) > 0
                ? '<div class="subtle">Legacy delete proposals still pending (' + Number(item.pending_delete_proposal_count || 0) + ')</div>'
                : '';
            var reviewSummary = figureReviewSummaryHtml(item);

            return '<details class="section-card figure-card" data-role="' + esc(item.figure_role || 'unknown') + '" data-rag-blocked="' + (ragBlocked ? "true" : "false") + '" ontoggle="if(this.open){loadFigureCardImage(this.querySelector(\'.figure-image-placeholder\'));}">' +
                   '<summary><div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;flex:1;width:100%;"><h3 style="margin:0;">' + figLabel + '</h3><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' + ragStatusHtml + directDeleteAction + codexAction + '</div></div></summary>' +
                   '<div style="margin-top:10px;">' +
                   imgHtml +
                   '<div class="prewrap" style="margin-top:12px;">' + esc(item.caption || "无 caption") + "</div>" +
                   summaryHtml + metaHtml + legacyDeleteNote + renderFigureParseDetailHtml(item) + reviewSummary + '</div></details>';
        }).join("");

        const figureNotice = noisyCount
            ? '<div class="section-card figure-audit-note"><h3>图表抽取提示</h3><div class="subtle">检测到 ' + noisyCount + ' 张自动截图疑似无效。IDE AI 回写核对时应把这些当作抽取噪声处理，不再把出版社标志、CrossMark 或页眉图片当作科学图表。</div></div>'
            : "";
        figureCards = figureNotice + filterHtml + cardsHtml;
    } else if (activeTab === "figures") {
        figureCards = '<div class="section-card"><h3>图片</h3><div class="muted">暂无内容。</div></div>';
    }

    const pdfEvidenceEntry =
        '<details class="section-card pdf-evidence-entry"><summary><h3>PDF 证据定位</h3></summary>' +
            '<div class="subtle" style="margin-bottom:12px;">' +
                '当前只支持有精确页码的证据跳转到 PDF，请使用右上角的“' + (paperHasPdf(detail) ? '查看 PDF / 证据定位' : 'PDF 未上传') + '”入口。<br>' +
                '如果下方仅显示文字，说明暂无精确的页码定位。' +
            '</div>' +
            '<div id="evidenceLocatorsPanel"><div class="muted">正在加载证据定位...</div></div>' +
        '</details>';

    let referenceCards = "";
    if (activeTab === "sections") {
        referenceCards = renderListBlock("参考文献", detail.references ? detail.references.slice(0, 20) : [], function(item) {
            return (
                '<div class="prewrap">' + esc(item.title || "未命名参考文献") + "</div>" +
                '<div class="subtle" style="margin-top:8px;">作者：' + esc(item.authors || "-") + " | DOI：" + esc(item.doi || "-") + "</div>" +
                (item.citation_context ? '<div class="mono" style="margin-top:8px;">' + esc(item.citation_context) + "</div>" : "")
            );
        });
    }

    const summaryEl = $("summaryContent");
    const sectionsEl = $("sectionsContent");
    const figuresEl = $("figuresContent");
    const dftEl = $("dftContent");
    const writingEl = $("writingContent");
    const translationEl = $("translationContent");
    const aggregateEl = $("aggregateResult");

    let missingPdfBanner = "";
    if (detail.oa_status === "metadata_only") {
        missingPdfBanner =
            '<div class="section-card" style="border: 1px dashed var(--color-warning); background: var(--color-warning-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-warning); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 尚无 PDF' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '当前文献仅包含元数据（标题、期刊、年份、DOI 等），尚未上传或关联实际 PDF 文献文件。' +
                '</p>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<button class="btn primary small" onclick="document.getElementById(\'attachPdfInput\').click()">上传 PDF 并自动合并</button>' +
                    '<input id="attachPdfInput" type="file" accept=".pdf" style="display: none;" onchange="attachPDFToPaperDetail(this, \'' + detail.id + '\')">' +
                '</div>' +
            '</div>';
    }

    let auditBanner = "";
    const reviewTabEl = $("tab-review");
    const existingWarning = $("reviewTabAuditWarning");
    if (existingWarning) existingWarning.remove();
    const existingAiTrail = $("aiAuditTrailPanel");
    if (existingAiTrail) existingAiTrail.remove();
    const existingPaperNotes = $("paperNotesPanel");
    if (existingPaperNotes) existingPaperNotes.remove();
    const existingTaskLog = $("taskLogPanel");
    if (existingTaskLog) existingTaskLog.remove();

    if (audit && (audit.stale > 0 || audit.ambiguous > 0 || audit.unresolved > 0)) {
        const totalAlerts = (audit.stale || 0) + (audit.ambiguous || 0) + (audit.unresolved || 0);
        auditBanner =
            '<div class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div class="subtle">请在审核中心或 DFT 数据库处理需要人工确认的数据；IDE AI 的总体意见会显示在“IDE AI 回写笔记”。</div>' +
            '</div>';

        const reviewTabWarningHtml =
            '<div id="reviewTabAuditWarning" class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div class="subtle">请在审核中心或 DFT 数据库处理需要人工确认的数据；IDE AI 的总体意见会显示在“IDE AI 回写笔记”。</div>' +
            '</div>';
        if (reviewTabEl) {
            reviewTabEl.insertAdjacentHTML("afterbegin", reviewTabWarningHtml);
        }
    }
    if (reviewTabEl && activeTab === "review") {
        reviewTabEl.insertAdjacentHTML("afterbegin", renderTaskLogPanel(detail, audit));
    }

    if (summaryEl) {
        summaryEl.innerHTML =
            missingPdfBanner +
            auditBanner +
            summaryCards +
            renderDetailTrustStrip(detail) +
            renderRagQualityPanel(detail) +
            baseInfo +
            pdfEvidenceEntry +
            localizedSummaryCard +
            abstractCard +
            comprehensiveCard;
        if (state.selectedPaperEvidenceLocators !== undefined) {
            renderEvidenceLocators(state.selectedPaperEvidenceLocators);
        }
    }
    if (sectionsEl && activeTab === "sections") {
        sectionsEl.innerHTML =
            renderManualReviewCompletionCard(detail, "content", "内容解析进度", "当摘要、章节和详情页展示内容都核对完毕后，再手动标记完成。若之后重新补解析，可随时取消。") +
            sectionCards +
            referenceCards +
            '<div style="margin-bottom:16px;">' +
            '<button class="btn primary small" onclick="promptAddRelationship(\'' + detail.id + '\')">绑定支撑文献</button>' +
            '</div>' +
            renderJSONCards("出向关系", detail.outgoing_relationships || []) +
            renderJSONCards("入向关系", detail.incoming_relationships || []);
    }
    if (figuresEl && activeTab === "figures") {
        figuresEl.innerHTML =
            renderManualReviewCompletionCard(detail, "figures", "图表进度", "当图表对象、裁图和关键信息都核对完毕后，再手动标记完成。若后续重新补图或重裁，可取消。") +
            figureCards +
            renderJSONCards("表格", detail.tables || []);
    }
    if (dftEl && activeTab === "dft") {
        const dftCandidateItems = dftResultsWithSafety(detail);
        const catalystSampleCards = dftCandidateItems.length
            ? ""
            : renderJSONCards("催化剂样本", detail.catalyst_samples_items || []);
        const catalystSamplesById = {};
        (detail.catalyst_samples_items || []).forEach(function(sample) {
            if (sample && sample.id) {
                catalystSamplesById[String(sample.id)] = sample;
            }
        });
        dftEl.innerHTML =
            renderDftExportReadiness(detail) +
            renderJSONCards("DFT 设置", detail.dft_settings_items || []) +
            catalystSampleCards +
            renderJSONCards("候选 DFT 数据", dftCandidateItems, { catalystSamplesById: catalystSamplesById }) +
            renderJSONCards("电化学性能", detail.electrochemical_performance_items || []) +
            renderJSONCards("机理声明", detail.mechanism_claims_items || []);
        decorateDftReadinessPanel(detail);
    }
    if (writingEl && activeTab === "writing") {
        const writingItems = detail.writing_cards_items || [];
        if (writingItems.length) {
            const reviewNotice = isAiVerifiedStatus(writingCardsReviewStatus)
                ? ""
                : '<div class="section-card figure-audit-note"><h3>\u5199\u4f5c\u5361\u72b6\u6001</h3><div class="subtle">\u8fd9\u6279\u5199\u4f5c\u5361\u8fd8\u6ca1\u8fdb\u5165 safe_verified\uff0c\u4f46\u73b0\u5728\u53ef\u4ee5\u76f4\u63a5\u67e5\u770b\u7814\u7a76\u7a7a\u767d\u3001\u62df\u89e3\u51b3\u65b9\u6848\u3001\u6838\u5fc3\u5047\u8bbe\u3001\u8bc1\u636e\u94fe\u548c\u5f53\u524d\u963b\u585e\u9879\u3002</div></div>';
            writingEl.innerHTML = reviewNotice + renderJSONCards("写作卡片", writingItems);
        } else {
            writingEl.innerHTML = renderPendingReviewCard("写作卡片", "\u5f53\u524d\u8fd8\u6ca1\u6709\u5199\u4f5c\u5361\u5185\u5bb9\u3002");
        }
    }
    if (false && writingEl && activeTab === "writing") {
        writingEl.innerHTML = isAiVerifiedStatus(writingCardsReviewStatus)
            ? renderJSONCards("写作卡片", detail.writing_cards_items || [])
            : renderPendingReviewCard("写作卡片", "写作卡待 AI 核验，不在详情页展示。");
    }
    if (translationEl && activeTab === "translation") {
        translationEl.innerHTML = (isAiVerifiedStatus(translationReviewStatus) || translationReviewStatus === "final_trusted")
            ? renderFullTranslation(detail)
            : renderPendingReviewCard("中文译文", "中文译文待 AI 核验或正式保存，不在详情页展示。");
    }
    if (aggregateEl) aggregateEl.innerHTML = "";
}
