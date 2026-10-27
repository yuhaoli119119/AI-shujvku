/* Stored Chinese reading help: no model calls on mount, refresh or tab navigation. */
window.PaperReading = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function invalidAbstract(paper) {
    const text=String(paper.reading_guide?.abstract_translation?.original_text || paper.abstract || '').normalize('NFKC').toLowerCase().replace(/\s+/g,' ').trim();
    return Boolean(paper.abstract_invalid_notice || (text.startsWith('this is a pdf file of an article') && text.includes('definitive version of record') && text.includes('copyediting') && text.includes('typesetting')));
  }
  const scopes = {abstract:'仅依据摘要',main_text:'依据正文文字与已有图表资料',main_and_si:'依据正文与 SI 文字、已有图表资料',partial_text:'依据部分文字资料'};
  let timer = null, paperId = null, currentJob = null, options = null;
  let epoch = 0, polls = 0, failures = 0, stopped = false, checking = false, readNote = '', generationError = '';
  let submitting = false;
  const MAX_POLLS = 40, MAX_FAILURES = 4;
  const isRunning = job => job && ['dispatching','running','queued'].includes(job.status);
  function stopTimer() { if(timer !== null) clearTimeout(timer); timer = null; }
  function schedule(delay) {
    stopTimer();
    if(polls >= MAX_POLLS || failures >= MAX_FAILURES) {
      stopped = true;
      readNote = '自动核对已暂停，点击“重新核对状态”继续；不会重新生成。';
      return;
    }
    timer = setTimeout(() => { timer = null; polls += 1; read(); }, delay);
  }
  const list = values => (Array.isArray(values) ? values : []).filter(v => typeof v === 'string' && v.trim());
  function section(title, value) {
    return typeof value === 'string' && value.trim() ? `<details class="reading-section"><summary>${title}</summary><p>${escape(value)}</p></details>` : '';
  }
  function render(paper) {
    const guide = paper.reading_guide?.beginner_zh;
    const quick = document.getElementById('pdQuickReading');
    const host = document.getElementById('pdBeginnerGuide');
    const notice = document.getElementById('pdReadingScope');
    if (!quick || !host) return;
    quick.hidden = !guide;
    if (!guide) {
      quick.innerHTML = '';
      notice.textContent = '尚未保存中文导读，可单独补充；原有摘要与资料仍可阅读。';
      host.innerHTML = '<p class="muted">补充后可阅读研究目标、创新点、关键对照、结论条件及文章论证脉络。</p>';
      return;
    }
    notice.textContent = (scopes[guide.content_scope] || '依据范围待核实') + ' · 阅读辅助，科学结论请结合原文判断' + (paper.reading_guide?.abstract_translation?.supplement_only?' · 中文摘要已单独补充，导读待补记录保留生成时状态':'');
    const short = text => { const value=String(text || ''); const first=value.indexOf('。'); return first>=0 ? value.slice(0,first+1) : value; };
    quick.innerHTML = `<div class="panel-title">30 秒读懂</div><p class="reading-takeaway">${escape(guide.one_sentence)}</p>${guide.problem ? `<p><strong>要解决什么问题</strong><br>${escape(short(guide.problem))}</p>`:''}${guide.main_finding ? `<p><strong>主要发现</strong><br>${escape(short(guide.main_finding))}</p>`:''}${short(guide.problem)!==guide.problem || short(guide.main_finding)!==guide.main_finding ? `<details class="reading-section"><summary>展开研究问题与主要发现的细节</summary><p>${escape(guide.problem)}</p><p>${escape(guide.main_finding)}</p></details>`:''}`;
    const g = guide.guide || {};
    const flow = list(guide.argument_flow);
    const evidence = Array.isArray(guide.evidence) ? guide.evidence.filter(e => /^[0-9a-f-]{36}$/i.test(e.asset_id || '')) : [];
    const sources = Array.isArray(guide.source_files) ? guide.source_files : [];
    const terms = Array.isArray(guide.terms) ? guide.terms : [];
    host.innerHTML = section('研究背景与空白',g.background_gap)+section('研究目标',g.research_objective)+section('研究方案与重要对照',g.study_design)+section('创新点与比较依据',g.innovation)+section('主要结论与适用条件',g.conclusions)+section('局限与待核实',g.limitations)+
      (flow.length ? `<section class="reading-flow"><h3>文章论证脉络</h3><ol>${flow.map(v=>`<li>${escape(v)}</li>`).join('')}</ol></section>`:'')+
      (evidence.length ? `<details class="reading-section"><summary>关键证据 · ${evidence.length} 个图表</summary>${evidence.map(e=>`<article class="reading-evidence"><h4>${escape(e.question)}</h4><p>${escape(e.finding)}</p><a href="index.html?paper_id=${encodeURIComponent(paper.id || paper.paper_id)}&tab=figures&target_type=rebuild_visual_asset&target_id=${encodeURIComponent(e.asset_id)}">查看对应图表与来源 →</a></article>`).join('')}</details>`:'')+
      (terms.length ? `<details class="reading-section"><summary>术语小词典</summary><dl>${terms.map(t=>`<dt>${escape(t.term)}</dt><dd>${escape(t.explanation)}</dd>`).join('')}</dl></details>`:'')+
      (list(guide.missing).length ? `<details class="reading-section"><summary>阅读范围与待补充</summary><ul>${list(guide.missing).map(s=>`<li>${escape(s)}</li>`).join('')}</ul></details>`:'')+
      (sources.length ? `<details class="reading-section"><summary>本次依据的文件</summary><ul>${sources.map(s=>`<li>${escape(s.role==='main'?'正文':'SI')} · ${escape(s.name)}${s.complete_text?'':'（文字不完整）'}</li>`).join('')}</ul></details>`:'');
  }
  function renderJob() {
    const btn=document.getElementById('pdGenerateReading'); const status=document.getElementById('pdReadingJob');
    if(!btn) return;
    const running=isRunning(currentJob);
    // Job status is historical progress; only this click's in-flight request
    // disables duplicate submission.
    btn.disabled=submitting;
    const abstractOnly=Boolean(options?.paper.reading_guide?.beginner_zh && (!options?.paper.abstract_zh || invalidAbstract(options.paper)));
    const abstractTask=currentJob?.scope==='abstract_only';
    btn.textContent=submitting?'正在提交…':abstractOnly?(invalidAbstract(options.paper)?'纠正中文摘要':'补充中文摘要'):running?'新建中文导读任务':'补充中文导读';
    const message=currentJob?.status==='failed' ? (currentJob.error || '本次未生成，可重试，已有资料已保留。') : running ?
      (currentJob.saved ? (abstractTask?'中文摘要已保存，可以阅读；任务正在收尾，正在核对完成状态。':'中文导读已保存，可以阅读；任务正在收尾，正在核对完成状态。') : '生成中，离开或刷新页面不会重复启动；已有结果不受影响。') : currentJob?.saved ? (abstractTask?'中文摘要已保存。':'中文导读已保存。') : '';
    status.textContent=[message,readNote,generationError].filter(Boolean).join(' ');
    const resume=document.getElementById('pdRefreshReadingState');
    if(resume){resume.hidden=!stopped;resume.disabled=checking;}
  }
  async function read() {
    stopTimer();
    const id=paperId, generation=epoch;
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),20000);
    checking=true;renderJob();
    try {
      const data=await options.fetch(`/api/rebuild/papers/${id}/reading-assistance`,{signal:controller.signal});
      if(id!==paperId || generation!==epoch)return;
      currentJob=data.job;failures=0;readNote='';
      if(data.guide){
        if(JSON.stringify(options.paper.reading_guide?.beginner_zh)!==JSON.stringify(data.guide)) {
          options.paper.reading_guide={...(options.paper.reading_guide || {}),beginner_zh:data.guide};render(options.paper);
        }
      }
      const translationChanged=JSON.stringify(options.paper.reading_guide?.abstract_translation)!==JSON.stringify(data.abstract_translation || undefined);
      if(translationChanged){options.paper.reading_guide={...(options.paper.reading_guide || {}),abstract_translation:data.abstract_translation};render(options.paper);}
      const validityChanged=options.paper.abstract_invalid_notice!==Boolean(data.abstract_invalid_notice);
      options.paper.abstract_invalid_notice=Boolean(data.abstract_invalid_notice);
      if(options.paper.abstract_zh!==data.abstract_zh || translationChanged || validityChanged){options.paper.abstract_zh=data.abstract_zh;options.abstract();}
      // Saving the content precedes the native terminal state: keep reading both stages.
      if(isRunning(currentJob)) schedule(Math.min(6000 + polls * 2000,15000));
      else { stopped=false;sessionStorage.removeItem('reading-help:'+id); }
    } catch(error) {
      if(id!==paperId || generation!==epoch)return;
      failures+=1;
      readNote='暂时无法读取状态，正在稍后重试；已有内容保留。';
      schedule(Math.min(6000 * 2 ** (failures-1),30000));
    } finally {
      clearTimeout(timeout);
      if(id===paperId && generation===epoch){checking=false;renderJob();}
    }
  }
  function resumeRead() {
    polls=0;failures=0;stopped=false;readNote='';
    return read();
  }
  async function generate() {
    if(submitting)return;
    const id=paperId, generation=epoch;
    const key='reading-help:'+id;
    let intent=sessionStorage.getItem(key);
    if(!intent){intent=crypto.randomUUID();sessionStorage.setItem(key,intent);}
    submitting=true;generationError='';renderJob();
    try {
      const me=await options.fetch('/api/auth/me');
      if(id!==paperId || generation!==epoch)return;
      const job=await options.fetch(`/api/rebuild/papers/${id}/reading-assistance/jobs`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf_token},body:JSON.stringify({request_id:intent,retry:Boolean(currentJob),scope:options.paper.reading_guide?.beginner_zh && (!options.paper.abstract_zh || invalidAbstract(options.paper))?'abstract_only':'full'})});
      sessionStorage.removeItem(key);
      if(id!==paperId || generation!==epoch)return;
      currentJob=job;
      renderJob();await read();
    } catch(error) {
      if(id!==paperId || generation!==epoch)return;
      if(error.status>=400 && error.status<500 && error.status!==429){
        generationError='暂未启动补充：'+(/[\u4e00-\u9fff]/.test(error.message)?error.message:'当前条件不满足，已有内容保留，请重新核对后再试。');
        renderJob();return;
      }
      readNote='本次生成请求尚未确认，正在核对原任务状态；已有内容保留。';
      schedule(6000);renderJob();
    } finally {
      if(id===paperId && generation===epoch){submitting=false;renderJob();}
    }
  }
  function mount(config) {
    stopTimer();epoch+=1;polls=0;failures=0;stopped=false;checking=false;submitting=false;readNote='';generationError='';currentJob=null;
    options=config;paperId=String(config.paper.id || config.paper.paper_id);
    render(config.paper);
    document.getElementById('pdGenerateReading').onclick=generate;
    document.getElementById('pdRefreshReadingState').onclick=resumeRead;
    read();
  }
  return {mount,render,invalidAbstract};
})();
