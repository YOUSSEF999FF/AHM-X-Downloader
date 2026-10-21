(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); try { tg.setHeaderColor('#0b0709'); tg.setBackgroundColor('#0b0709'); } catch (_) {} }
  const initData = tg?.initData || '';
  const $ = (id) => document.getElementById(id);
  const state = { user:null, isAdmin:false, isOwner:false, settings:{}, usage:{}, analysis:null, selectedChoice:null, currentJob:null, poller:null, historyOffset:0, historyHasMore:false, usersOffset:0, usersHasMore:false };
  const formatSize = (bytes) => {
    const n = Number(bytes || 0); if (!n) return 'الحجم غير معروف';
    if (n < 1024*1024) return `${(n/1024).toFixed(0)} KB`;
    return n < 1024**3 ? `${(n/1024**2).toFixed(1)} MB` : `${(n/1024**3).toFixed(2)} GB`;
  };
  async function api(path, options={}) {
    const headers = { 'X-Telegram-Init-Data': initData, ...(options.headers || {}) };
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, {...options, headers});
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      let message = data.detail || data.message || 'حصل خطأ أثناء تنفيذ الطلب.';
      if (typeof message === 'object') message = message.message || JSON.stringify(message);
      if (data.error_id && !String(message).includes(data.error_id)) message = `${message} · Diagnostic: ${data.error_id}`;
      const err = new Error(String(message)); err.status = res.status; err.data = data.detail; err.errorId = data.error_id || ''; throw err;
    }
    return data;
  }
  let toastTimer;
  function toast(message, kind='') {
    const el = $('toast'); el.textContent = message; el.className = `toast show ${kind}`;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = 'toast'; }, 3700);
  }
  function setBusy(button, busy, busyText='جاري التنفيذ…') {
    if (!button) return;
    if (busy) { button.dataset.oldText = button.querySelector('span')?.textContent || button.textContent; button.disabled = true; if (button.querySelector('span')) button.querySelector('span').textContent = busyText; }
    else { button.disabled = false; if (button.querySelector('span') && button.dataset.oldText) button.querySelector('span').textContent = button.dataset.oldText; }
  }
  function showTab(tab) {
    document.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.dataset.tab === tab));
    $('downloadView').classList.toggle('active-view', tab === 'download'); $('downloadView').classList.toggle('hidden', tab !== 'download');
    $('historyView').classList.toggle('active-view', tab === 'history'); $('historyView').classList.toggle('hidden', tab !== 'history');
    $('adminView').classList.toggle('active-view', tab === 'admin'); $('adminView').classList.toggle('hidden', tab !== 'admin');
    if (tab === 'history') loadHistory(); if (tab === 'admin' && state.isAdmin) loadAdminSettings();
  }
  document.querySelectorAll('.tab').forEach(el => el.addEventListener('click', () => showTab(el.dataset.tab)));
  function updateUsage(usage) {
    if (!usage) return; state.usage = usage;
    $('usedToday').textContent = Number(usage.completed_today || 0) + Number(usage.active_today || 0);
    $('dailyLimit').textContent = usage.daily_limit ?? state.settings.daily_limit ?? 8;
    $('remainingToday').textContent = usage.remaining_today ?? 0;
    $('totalDownloads').textContent = usage.completed_total ?? 0;
    const pct = usage.daily_limit ? Math.min(100, ((usage.completed_today + usage.active_today) / usage.daily_limit) * 100) : 0;
    $('dailyProgress').style.width = `${pct}%`;
  }
  function qualityTile(fmt) {
    const button = document.createElement('button'); button.type = 'button'; button.className = `format-option${state.selectedChoice === fmt.choice ? ' selected' : ''}`;
    const mark = document.createElement('span'); mark.className = 'format-mark'; mark.textContent = fmt.choice === 'audio' ? 'MP3' : fmt.label;
    const content = document.createElement('span'); content.className = 'format-content';
    const label = document.createElement('span'); label.className = 'format-label'; label.textContent = fmt.choice === 'audio' ? 'استخراج الصوت' : `فيديو ${fmt.label}`;
    const size = document.createElement('span'); size.className = 'format-size'; size.textContent = fmt.size_text || formatSize(fmt.size);
    const check = document.createElement('span'); check.className = 'format-check'; check.textContent = '✓';
    content.append(label,size); button.append(mark,content,check);
    button.addEventListener('click', () => { state.selectedChoice = fmt.choice; document.querySelectorAll('.format-option').forEach(n => n.classList.remove('selected')); button.classList.add('selected'); $('downloadButton').disabled = false; });
    return button;
  }
  async function analyze() {
    const url = $('urlInput').value.trim(); if (!url) return toast('الصق رابط الفيديو الأول.', 'bad');
    setBusy($('analyzeButton'), true, 'جاري تحليل الرابط…'); $('videoCard').classList.add('hidden'); $('jobPanel').classList.add('hidden'); state.analysis = null; state.selectedChoice = null;
    try {
      const data = await api('/api/analyze', {method:'POST', body:JSON.stringify({url})});
      state.analysis = data.video; state.selectedChoice = data.video.formats?.[0]?.choice || null;
      renderVideo(data.video); $('videoCard').classList.remove('hidden'); $('videoCard').scrollIntoView({behavior:'smooth',block:'start'}); toast('تم تحليل الرابط بنجاح.', 'good');
    } catch (err) { toast(err.message, 'bad'); if (err.status === 503) setMaintenanceBanner(); }
    finally { setBusy($('analyzeButton'), false); updateUsageSafe(); }
  }
  function renderVideo(video) {
    $('videoTitle').textContent = video.title || 'فيديو بدون عنوان'; $('videoMeta').textContent = `${video.uploader || 'ناشر غير معروف'} · ${video.duration || 'المدة غير متاحة'}${video.views ? ` · ${Number(video.views).toLocaleString()} مشاهدة` : ''}`;
    $('platformBadge').textContent = String(video.platform || 'VIDEO').toUpperCase();
    $('durationBadge').textContent = video.duration || '--:--';
    const thumb = $('thumbnail');
    if (video.thumbnail && /^https:\/\//i.test(video.thumbnail)) { thumb.src = video.thumbnail; thumb.classList.remove('hidden'); $('thumbFallback').classList.add('hidden'); thumb.onerror = () => { thumb.classList.add('hidden'); $('thumbFallback').classList.remove('hidden'); }; }
    else { thumb.removeAttribute('src'); thumb.classList.add('hidden'); $('thumbFallback').classList.remove('hidden'); }
    if (video.note) { $('providerNote').textContent = video.note; $('providerNote').classList.remove('hidden'); } else $('providerNote').classList.add('hidden');
    const formats = $('formatOptions'); formats.replaceChildren();
    (video.formats || []).forEach(fmt => formats.appendChild(qualityTile(fmt)));
    $('downloadButton').disabled = !state.selectedChoice || !video.formats?.length;
  }
  function renderJob(job) {
    $('jobPanel').classList.remove('hidden'); $('jobPhase').textContent = job.phase || 'جاري التنفيذ…';
    const pct = Math.max(0, Math.min(100, Number(job.progress || 0)));
    $('jobPercent').textContent = `${Math.floor(pct)}%`; $('jobProgress').style.width = `${pct}%`;
    const d = Number(job.downloaded_bytes || 0), t = Number(job.total_bytes || 0);
    $('jobBytes').textContent = `${formatSize(d)} / ${t ? formatSize(t) : 'حجم غير معروف'}`;
    $('jobSpeed').textContent = job.speed || job.platform || 'جارٍ التحميل';
    $('cancelJobButton').classList.toggle('hidden', !['queued','running'].includes(job.status));
    $('saveFileButton').classList.toggle('hidden', job.status !== 'completed');
    $('jobMessage').classList.toggle('hidden', !['failed','cancelled'].includes(job.status));
    if (job.status === 'failed') $('jobMessage').textContent = `فشل التنزيل. رقم التشخيص: ${job.error_id || 'غير متاح'} — ${job.error_message || ''}`;
    else if (job.status === 'cancelled') $('jobMessage').textContent = 'تم إلغاء العملية.';
    else if (job.status === 'completed') $('jobMessage').textContent = 'اكتمل التنزيل. اضغط حفظ الملف لنقله إلى جهازك.';
  }
  async function startDownload() {
    if (!state.analysis || !state.selectedChoice) return toast('اختار الجودة الأول.', 'bad');
    setBusy($('downloadButton'), true, 'جاري بدء التنزيل…');
    try {
      const data = await api('/api/download', {method:'POST', body:JSON.stringify({analysis_id:state.analysis.analysis_id, choice:state.selectedChoice})});
      state.currentJob = data.job_id; $('jobPanel').classList.remove('hidden'); renderJob({status:'queued',phase:'تم وضع طلبك في قائمة الانتظار',progress:0,downloaded_bytes:0,total_bytes:0});
      $('jobPanel').scrollIntoView({behavior:'smooth',block:'start'}); beginPolling(data.job_id); toast('بدأنا تجهيز الملف.', 'good');
    } catch (err) { toast(err.message, 'bad'); if (err.status === 503) setMaintenanceBanner(); }
    finally { setBusy($('downloadButton'), false); }
  }
  function beginPolling(jobId) {
    if (state.poller) clearInterval(state.poller);
    let pollCount = 0;
    const poll = async () => {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`); renderJob(job); pollCount += 1;
        if (pollCount % 5 === 0 || ['completed','failed','cancelled'].includes(job.status)) await updateUsageSafe();
        if (['completed','failed','cancelled'].includes(job.status)) { clearInterval(state.poller); state.poller = null; state.currentJob = jobId; loadHistory(false); if (job.status === 'completed') toast('اكتمل الملف — تقدر تحفظه دلوقتي.', 'good'); }
      } catch (err) { clearInterval(state.poller); state.poller = null; toast(err.message, 'bad'); }
    };
    poll(); state.poller = setInterval(poll, 1300);
  }
  async function saveFile() {
    if (!state.currentJob) return;
    const button = $('saveFileButton'); setBusy(button, true, 'جاري تجهيز الملف…');
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(state.currentJob)}/file`, {headers:{'X-Telegram-Init-Data':initData}});
      if (!response.ok) { let d={}; try {d=await response.json();} catch (_){} throw new Error(d.detail || 'الملف غير متاح أو انتهت صلاحيته.'); }
      const blob = await response.blob(); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'AHMX-download'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30_000); toast('بدأ حفظ الملف على جهازك.', 'good');
    } catch (err) { toast(err.message, 'bad'); }
    finally { setBusy(button, false); }
  }
  async function cancelJob() {
    if (!state.currentJob) return;
    try { await api(`/api/jobs/${encodeURIComponent(state.currentJob)}/cancel`, {method:'POST'}); toast('وصل طلب الإلغاء.'); }
    catch (err) { toast(err.message, 'bad'); }
  }
  function statusLabel(status) { return ({completed:'مكتمل',failed:'فشل',queued:'في الانتظار',running:'جاري التحميل',cancelled:'أُلغي'})[status] || status; }
  async function loadHistory(append=false) {
    try {
      if (!append) state.historyOffset = 0;
      const data = await api(`/api/history?limit=50&offset=${state.historyOffset}`); updateUsage(data.usage);
      const list = $('historyList'); if (!append) list.replaceChildren();
      if (!data.items?.length && !append) { list.innerHTML = '<div class="empty-state"><span>◷</span><b>لسه مفيش تنزيلات</b><small>أول تنزيل ليك هيظهر هنا.</small></div>'; }
      data.items.forEach(item => {
        const row = document.createElement('div'); row.className = 'history-item';
        const icon = document.createElement('div'); icon.className = 'history-icon'; icon.textContent = item.choice === 'audio' ? '♫' : '▶';
        const info = document.createElement('div'); info.className = 'history-info';
        const title = document.createElement('div'); title.className = 'history-title'; title.textContent = item.title || 'فيديو بدون عنوان';
        const sub = document.createElement('div'); sub.className = 'history-sub'; sub.textContent = `${item.platform} · ${item.choice === 'audio' ? 'MP3' : item.choice+'p'} · ${formatSize(item.file_size)} · ${new Date(item.created_at).toLocaleString()}`;
        const status = document.createElement('div'); status.className = `history-status status-${item.status}`; status.textContent = statusLabel(item.status);
        info.append(title,sub); row.append(icon,info,status); list.appendChild(row);
      });
      state.historyOffset = data.next_offset || (state.historyOffset + (data.items||[]).length); state.historyHasMore = !!data.has_more;
      $('loadMoreHistoryButton').classList.toggle('hidden', !state.historyHasMore);
    } catch (err) { toast(err.message, 'bad'); }
  }
  async function updateUsageSafe() { try { const d = await api('/api/bootstrap'); updateUsage(d.usage); } catch (_) {} }
  function setMaintenanceBanner() { $('welcomeText').textContent = 'الخدمة في وضع الصيانة حاليًا. جرّب لاحقًا.'; }
  async function loadBootstrap() {
    try {
      const data = await api('/api/bootstrap'); state.user = data.user; state.isAdmin = data.is_admin; state.isOwner = data.is_owner; state.settings = data.settings || {}; updateUsage(data.usage);
      $('welcomeText').textContent = data.maintenance ? 'الخدمة في وضع الصيانة حاليًا.' : `أهلًا ${data.user.name || ''}، حط الرابط وابدأ.`;
      $('analyzeButton').disabled = !!data.maintenance && !data.is_admin; if (data.maintenance && !data.is_admin) { $('downloadButton').disabled = true; toast('الخدمة تحت الصيانة حاليًا.', 'bad'); } else if (state.analysis?.formats?.length) { $('downloadButton').disabled = !state.selectedChoice; }
      if (data.is_admin) $('adminTab').classList.remove('hidden');
    } catch (err) {
      $('welcomeText').textContent = 'افتح التطبيق من زر تيليجرام الرسمي للتحقق من هويتك.';
      $('analyzeButton').disabled = true; toast(`تعذر التحقق من جلسة Telegram: ${err.message}`, 'bad');
    }
  }
  async function loadAdminSettings() {
    try {
      const data = await api('/api/admin/settings'); state.settings = data.settings;
      const settings = data.settings;
      ['maintenance_mode','downloads_enabled','youtube_enabled','facebook_enabled','instagram_enabled','generic_enabled','audio_enabled','owner_alerts_enabled','allow_private_chats_only'].forEach(key => { const el=$(`setting_${key}`); if(el) el.checked=!!settings[key]; });
      ['max_concurrent','daily_limit','max_upload_mb','max_video_height','cooldown_seconds'].forEach(key => { const el=$(`setting_${key}`); if(el) el.value=settings[key]; });
      const stats = $('statsGrid'); stats.replaceChildren();
      [['users','مستخدمون'],['today_jobs','طلبات اليوم'],['completed','مكتمل'],['failed','فشل'],['active','نشط'],['errors','أخطاء']].forEach(([k,label]) => { const card=document.createElement('div');card.className='stat-card';const n=document.createElement('strong');n.textContent=data.stats[k]??0;const l=document.createElement('span');l.textContent=label;card.append(n,l);stats.appendChild(card); });
      $('adminOwnerTools').classList.toggle('hidden', !state.isOwner);
      if (state.isOwner) renderAdmins(data.admins || []);
      await loadUsers();
    } catch (err) { toast(err.message, 'bad'); }
  }
  async function saveAdminSettings() {
    const keysBool=['maintenance_mode','downloads_enabled','youtube_enabled','facebook_enabled','instagram_enabled','generic_enabled','audio_enabled','owner_alerts_enabled','allow_private_chats_only'];
    const patch={}; keysBool.forEach(key => patch[key]=$(`setting_${key}`).checked);
    ['max_concurrent','daily_limit','max_upload_mb','max_video_height','cooldown_seconds'].forEach(key => patch[key]=Number($(`setting_${key}`).value));
    setBusy($('saveSettingsButton'), true, 'جاري الحفظ…');
    try { const data=await api('/api/admin/settings',{method:'PATCH',body:JSON.stringify(patch)});state.settings=data.settings;$('settingsMessage').textContent='✅ تم حفظ الإعدادات. التعديلات أصبحت سارية فورًا.';$('settingsMessage').classList.remove('hidden');toast('تم حفظ الإعدادات.', 'good');await loadBootstrap(); }
    catch(err){$('settingsMessage').textContent=`❌ ${err.message}`;$('settingsMessage').classList.remove('hidden');toast(err.message,'bad');}
    finally{setBusy($('saveSettingsButton'),false);}
  }
  async function loadErrors() {
    try {
      const data=await api('/api/admin/errors'); const list=$('errorsList'); list.replaceChildren();
      if (!data.items?.length) { list.innerHTML='<div class="empty-state"><span>✓</span><b>لا توجد أخطاء محفوظة</b><small>السجل نظيف حاليًا.</small></div>';return; }
      data.items.forEach(err=>{const card=document.createElement('div');card.className='error-item';const head=document.createElement('div');head.className='error-title';const code=document.createElement('span');code.textContent=`${err.error_id} · ${err.source}`;const type=document.createElement('span');type.textContent=err.error_type;head.append(code,type);const msg=document.createElement('div');msg.className='error-msg';msg.textContent=`${err.created_at} · ${err.platform||'غير محدد'} · ${err.source_hint||''}\n${err.message||''}`;const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='عرض Stack Trace';const trace=document.createElement('pre');trace.className='error-trace';trace.textContent=err.traceback_text||'لم يتم حفظ Stack Trace';details.append(summary,trace);card.append(head,msg,details);list.appendChild(card);});
    } catch(err){toast(err.message,'bad');}
  }
  function renderAdmins(admins) {
    const list=$('adminsList'); list.replaceChildren();
    if(!admins.length){list.innerHTML='<div class="empty-state"><small>قائمة المشرفين غير متاحة.</small></div>';return;}
    admins.forEach(a=>{const row=document.createElement('div');row.className='admin-id';const label=document.createElement('span');label.textContent=`ID: ${a.user_id} ${a.user_id===state.user?.id?'(أنت)':''}`;const date=document.createElement('small');date.textContent=a.added_at || '';row.append(label,date);if(a.user_id!==state.user?.id){const remove=document.createElement('button');remove.className='small-action';remove.textContent='إزالة';remove.addEventListener('click',()=>removeAdmin(a.user_id));row.appendChild(remove);}list.appendChild(row);});
  }
  async function addAdmin(){const id=Number($('newAdminId').value);if(!id)return toast('أدخل Telegram User ID صحيحًا.','bad');try{const data=await api('/api/admin/admins',{method:'POST',body:JSON.stringify({user_id:id})});renderAdmins(data.admins);$('newAdminId').value='';toast('تمت إضافة المشرف.','good');}catch(err){toast(err.message,'bad');}}
  async function loadUsers(append=false){
    const offset=append?state.usersOffset:0;
    try{
      const data=await api(`/api/admin/users?limit=30&offset=${offset}`);
      renderUsers(data.items||[],append);
      state.usersOffset=data.next_offset??(offset+(data.items||[]).length);state.usersHasMore=!!data.has_more;
      $('loadMoreUsersButton').classList.toggle('hidden',!state.usersHasMore);
    }catch(err){toast(err.message,'bad');}
  }
  function renderUsers(users,append=false){
    const list=$('usersList');if(!append)list.replaceChildren();
    if(!users.length&&!append){list.innerHTML='<div class="empty-state"><small>لا يوجد مستخدمون مسجلون حتى الآن.</small></div>';return;}
    users.forEach(u=>{
      const row=document.createElement('div');row.className='user-admin-row';
      const main=document.createElement('div');main.className='user-admin-main';
      const name=document.createElement('b');name.textContent=(u.first_name||u.username||'مستخدم')+(u.username?` · @${u.username}`:'');
      const meta=document.createElement('small');meta.textContent=`ID ${u.user_id} · اليوم ${u.completed_today||0} · الإجمالي ${u.completed_total||0} · فشل ${u.failed_total||0}`;
      const quota=document.createElement('small');quota.textContent=`المتبقي ${u.remaining_today??0} من ${u.effective_daily_limit??8}${u.daily_limit_override?' · حد مخصص':''}`;
      main.append(name,meta,quota);row.append(main);
      const actions=document.createElement('div');actions.className='user-admin-actions';
      if(!u.is_admin&&!u.is_owner){
        const block=document.createElement('button');block.className='small-action';block.textContent=u.blocked?'فك الحظر':'حظر';block.addEventListener('click',()=>updateUser(u.user_id,{blocked:!u.blocked}));actions.appendChild(block);
        const limit=document.createElement('button');limit.className='small-action';limit.textContent='الحد اليومي';limit.addEventListener('click',()=>setUserLimit(u));actions.appendChild(limit);
      }else{const protectedLabel=document.createElement('small');protectedLabel.className='user-protected';protectedLabel.textContent=u.is_owner?'المالك':'مشرف';actions.appendChild(protectedLabel);}
      row.append(actions);list.appendChild(row);
    });
  }
  async function updateUser(id,patch){
    const action=patch.blocked===true?'إيقاف استخدام هذا الحساب':patch.blocked===false?'فك حظر هذا الحساب':'تعديل هذا المستخدم';
    if(!confirm(`${action}؟`))return;
    try{await api(`/api/admin/users/${id}`,{method:'PATCH',body:JSON.stringify(patch)});toast('تم تحديث صلاحيات المستخدم.','good');await loadUsers();await loadAdminSettings();}
    catch(err){toast(err.message,'bad');}
  }
  async function setUserLimit(user){
    const current=user.daily_limit_override==null?'':String(user.daily_limit_override);
    const answer=prompt('اكتب الحد اليومي من 1 إلى 100. اتركه فارغًا لاستخدام الحد العام:',current);
    if(answer===null)return;
    let limit=null;
    if(answer.trim()!==''){limit=Number(answer);if(!Number.isInteger(limit)||limit<1||limit>100)return toast('اكتب رقمًا صحيحًا من 1 إلى 100 أو اتركه فارغًا.','bad');}
    try{await api(`/api/admin/users/${user.user_id}`,{method:'PATCH',body:JSON.stringify({daily_limit_override:limit})});toast('تم تحديث الحد اليومي.','good');await loadUsers();}
    catch(err){toast(err.message,'bad');}
  }
  async function removeAdmin(id){if(!confirm(`إزالة صلاحيات ${id}؟`))return;try{const data=await api(`/api/admin/admins/${id}`,{method:'DELETE'});renderAdmins(data.admins);toast('تمت إزالة المشرف.','good');}catch(err){toast(err.message,'bad');}}
  $('analyzeButton').addEventListener('click', analyze); $('downloadButton').addEventListener('click', startDownload); $('saveFileButton').addEventListener('click', saveFile); $('cancelJobButton').addEventListener('click', cancelJob);
  $('refreshButton').addEventListener('click', async()=>{await loadBootstrap();if(state.isAdmin)await loadAdminSettings();toast('تم تحديث المعلومات.','good');});
  $('historyRefresh').addEventListener('click', () => loadHistory(false)); $('loadMoreHistoryButton').addEventListener('click', () => loadHistory(true)); $('saveSettingsButton').addEventListener('click', saveAdminSettings); $('loadErrorsButton').addEventListener('click', loadErrors); $('loadUsersButton').addEventListener('click', () => loadUsers(false)); $('loadMoreUsersButton').addEventListener('click', () => loadUsers(true)); $('addAdminButton').addEventListener('click', addAdmin);
  $('pasteButton').addEventListener('click', async()=>{try{$('urlInput').value=await navigator.clipboard.readText();toast('تم لصق الرابط.','good');}catch(_){$('urlInput').focus();toast('استخدم لصق من لوحة مفاتيح الموبايل.');}});
  $('urlInput').addEventListener('keydown',e=>{if(e.key==='Enter')analyze();});
  loadBootstrap();
})();
