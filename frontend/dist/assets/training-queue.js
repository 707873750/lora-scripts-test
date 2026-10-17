/** FIFO training queue controls, shared by all vendored GUI routes. */
(function () {
  if (document.getElementById('training-queue-toggle')) return;
  const style = document.createElement('style');
  style.textContent = `
    #training-queue-toggle{position:fixed;right:24px;bottom:24px;z-index:3000;background:#2563eb;color:white;border:0;border-radius:24px;padding:12px 20px;cursor:pointer;box-shadow:0 3px 12px #0003}
    #training-queue-panel{position:fixed;right:24px;bottom:80px;z-index:3001;width:min(620px,calc(100vw - 48px));max-height:70vh;overflow:auto;background:var(--c-bg,#fff);color:var(--c-text,#303133);border:1px solid #8886;border-radius:12px;padding:20px;box-sizing:border-box;box-shadow:0 4px 24px #0003}
    #training-queue-panel header{display:flex;justify-content:space-between;align-items:center}#training-queue-panel h2{margin:0;font-size:20px}#training-queue-panel p{font-size:13px}
    #training-queue-panel article{border-top:1px solid #8884;padding:12px 0;display:flex;gap:12px;justify-content:space-between}#training-queue-panel .queue-info{min-width:0;overflow-wrap:anywhere}#training-queue-panel small{display:block;opacity:.7}#training-queue-panel button{cursor:pointer;padding:5px 10px}#training-queue-panel .shutdown-controls{border-top:1px solid #8884;padding:12px 0}#training-queue-panel .shutdown-controls label{display:flex;gap:8px;align-items:center}#training-queue-panel .shutdown-controls input{width:16px;height:16px}#training-queue-panel .shutdown-state{font-weight:600}#training-queue-panel .queue-actions{display:flex;gap:8px;align-items:center;flex-shrink:0}
  `;
  document.head.append(style);
  const toggle = document.createElement('button');
  toggle.id = 'training-queue-toggle';
  toggle.textContent = '训练队列';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', 'training-queue-panel');
  const panel = document.createElement('section');
  panel.id = 'training-queue-panel';
  panel.hidden = true;
  panel.setAttribute('aria-label', '训练队列');
  panel.innerHTML = '<header><h2>训练队列</h2><button type="button" aria-label="关闭训练队列">关闭</button></header><p>按提交顺序依次训练，失败或取消后继续下一项。队列仅保存在本次服务会话中，重启后清空。</p><div class="shutdown-controls"><label><input type="checkbox" class="shutdown-enabled" disabled>所有训练任务结束后自动关机</label><p>关闭运行训练服务的电脑。失败或取消也算结束；队列结束后倒计时 60 秒，期间可取消。新提交任务会撤销倒计时，等队列结束后重新计时。</p><p class="shutdown-state" role="status">正在读取关机设置…</p><button type="button" class="shutdown-cancel" hidden>取消自动关机</button></div><p class="queue-message" role="status"></p><div class="queue-list"></div>';
  document.body.append(toggle, panel);
  const list = panel.querySelector('.queue-list');
  const message = panel.querySelector('.queue-message');
  const shutdownCheckbox = panel.querySelector('.shutdown-enabled');
  const shutdownMessage = panel.querySelector('.shutdown-state');
  const shutdownCancel = panel.querySelector('.shutdown-cancel');
  let shutdownState = null;
  let savingShutdown = false;
  let shutdownRevision = 0;
  let shutdownError = '';
  let wasCountingDown = false;
  function renderShutdown(state) {
    if (!state) return;
    shutdownState = state;
    if (!savingShutdown) shutdownCheckbox.checked = state.enabled;
    shutdownCheckbox.disabled = savingShutdown || !state.supported;
    shutdownCancel.disabled = savingShutdown;
    shutdownCancel.hidden = state.remaining_seconds == null;
    const countingDown = state.remaining_seconds != null;
    if (countingDown && !wasCountingDown) {
      panel.hidden = false;
      toggle.setAttribute('aria-expanded', 'true');
    }
    wasCountingDown = countingDown;
    shutdownMessage.textContent = shutdownError || state.error || (
      !state.supported ? '当前平台不支持自动关机' :
      state.triggered ? '已发送关机命令，无法取消' :
      countingDown ? `队列已结束，${state.remaining_seconds} 秒后关机。可关闭开关或点击取消。` :
      state.enabled ? '已开启：等待训练队列结束。关闭浏览器后仍然有效；重启服务后关闭。' :
      '自动关机已关闭');
  }
  async function saveShutdown(enabled) {
    if (savingShutdown) return;
    savingShutdown = true;
    shutdownRevision++;
    shutdownError = '';
    shutdownCheckbox.disabled = true;
    shutdownCancel.disabled = true;
    try {
      const response = await fetch('/api/tasks/shutdown', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enabled}), signal:AbortSignal.timeout(15000),
      });
      const result = await response.json();
      if (!response.ok || result.status !== 'success') throw new Error(result.message || '保存自动关机设置失败');
      shutdownState = result.data.shutdown;
    } catch (error) { shutdownError = error.message; }
    finally { shutdownRevision++; savingShutdown = false; renderShutdown(shutdownState); await refresh(); }
  }
  shutdownCheckbox.onchange = () => saveShutdown(shutdownCheckbox.checked);
  shutdownCancel.onclick = () => saveShutdown(false);
  const labels = {QUEUED:'排队中',RUNNING:'训练中',FINISHED:'已完成',FAILED:'失败',TERMINATED:'已取消',CREATED:'已创建'};
  function close() { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); toggle.focus(); }
  panel.querySelector('header button').onclick = close;
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !panel.hidden) close(); });
  toggle.onclick = () => { panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden)); if (!panel.hidden) refresh(); };
  let busy = false;
  let lastRendered = null;
  let actionError = "";
  const cancelling = new Set();
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const revision = shutdownRevision;
      const response = await fetch('/api/tasks', {cache:'no-store', signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('无法获取训练队列');
      const payload = await response.json();
      const tasks = payload.data.tasks;
      if (revision === shutdownRevision && !savingShutdown) renderShutdown(payload.data.shutdown);
      const active = tasks.filter(t => ['QUEUED','RUNNING'].includes(t.status));
      toggle.textContent = `训练队列 (${active.length})` + (shutdownState?.remaining_seconds != null ? ` · ${shutdownState.remaining_seconds} 秒后关机` : "");
      if (panel.hidden) return;
      message.textContent = actionError || (tasks.length ? `训练中 ${tasks.filter(t=>t.status==='RUNNING').length} · 等待 ${tasks.filter(t=>t.status==='QUEUED').length}` : '暂无任务。可在训练页面连续提交多个配置。');
      const snapshot = JSON.stringify(tasks);
      if (snapshot === lastRendered) return;
      lastRendered = snapshot;
      list.replaceChildren();
      for (const task of [...active, ...tasks.filter(t => !active.includes(t)).reverse()]) {
        const row = document.createElement('article');
        const info = document.createElement('div'); info.className = 'queue-info';
        const title = document.createElement('strong'); title.textContent = task.name || task.id;
        const state = document.createElement('div'); state.textContent = (labels[task.status] || task.status) + (task.queue_position ? ` · 第 ${task.queue_position} 位` : '');
        const id = document.createElement('small'); id.textContent = task.id;
        info.append(title, state, id);
        const actions = document.createElement('div'); actions.className = 'queue-actions';
        const log = document.createElement('a'); log.href = '/train-log?task_id=' + encodeURIComponent(task.id); log.target = '_blank'; log.rel = 'noopener'; log.textContent = '日志'; actions.append(log);
        if (['QUEUED','RUNNING'].includes(task.status)) {
          const cancel = document.createElement('button'); cancel.textContent = task.status === 'QUEUED' ? '取消排队' : '停止训练';
          cancel.disabled = cancelling.has(task.id);
          cancel.onclick = async () => {
            if (cancelling.has(task.id)) return;
            actionError = "";
            cancelling.add(task.id);
            cancel.disabled = true;
            try {
              const response = await fetch('/api/tasks/terminate/' + encodeURIComponent(task.id), {signal:AbortSignal.timeout(15000)});
              const result = await response.json();
              if (!response.ok || result.status !== 'success') throw new Error('取消任务失败');
              await refresh();
            } catch (error) { actionError = error.message; message.textContent = actionError; }
            finally { cancelling.delete(task.id); cancel.disabled = false; lastRendered = null; await refresh(); }
          };
          actions.append(cancel);
        }
        row.append(info, actions); list.append(row);
      }
    } catch (error) { if (!panel.hidden) message.textContent = error.message; }
    finally { busy = false; }
  }
  refresh();
  setInterval(refresh, 2000);
})();
