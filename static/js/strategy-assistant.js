(() => {
    'use strict';
    const config = JSON.parse(document.getElementById('assistantConfig').textContent);
    const app = document.getElementById('strategyAssistantApp');
    const messages = document.getElementById('assistantMessages');
    const transcript = document.getElementById('assistantTranscript');
    const composer = document.getElementById('assistantComposer');
    const input = document.getElementById('assistantInput');
    const sendButton = document.getElementById('assistantSend');
    const retryButton = document.getElementById('assistantRetry');
    const errorBox = document.getElementById('assistantError');
    const status = document.getElementById('assistantStatus');
    const proposalBox = document.getElementById('assistantProposal');
    const sessionList = document.getElementById('assistantSessionList');
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const base = `/api/v2/workspaces/${config.workspace_id}`;
    let sessionId = null;
    let strategy = null;
    let editTarget = null;
    let contextReady = false;
    let conversation = null;
    let epoch = 0;
    let busy = false;
    let retryId = null;
    let lastUserText = '';
    let sessionPage = 1;
    let sessionItems = [];
    let searchQuery = '';
    let attachableStrategies = [];

    async function request(path, options = {}) {
        const response = await fetch(path, {credentials: 'same-origin', ...options,
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken, ...(options.headers || {})}});
        let payload;
        try { payload = await response.json(); }
        catch (_) { throw new Error('服务器返回了无法读取的内容，请稍后重试。'); }
        if (!response.ok || payload.success === false) {
            const error = new Error(payload.message || '请求失败，请稍后重试。');
            error.payload = payload;
            throw error;
        }
        return payload;
    }
    const text = (tag, value, className) => {
        const node = document.createElement(tag); node.textContent = value;
        if (className) node.className = className;
        return node;
    };
    const button = (label, action, primary = false) => {
        const node = text('button', label, `assistant-button${primary ? ' primary' : ''}`);
        node.type = 'button'; node.addEventListener('click', action); return node;
    };
    function showError(message) { errorBox.textContent = message || ''; errorBox.hidden = !message; }
    function setBusy(value) {
        busy = value; sendButton.disabled = value || !contextReady || conversation?.session?.can_edit === false;
        retryButton.disabled = value || !contextReady; input.readOnly = value || !contextReady || conversation?.session?.can_edit === false;
        document.getElementById('assistantNew').disabled = value || !contextReady;
        document.getElementById('assistantNewStrategy').disabled = value || !contextReady;
        for (const node of document.querySelectorAll('.assistant-session-delete')) node.disabled = value;
        document.querySelector('#assistantAttachForm button').disabled = value || !contextReady;
        document.getElementById('assistantThinking').hidden = !value;
        status.textContent = value ? '正在等待 AI 回复…' : !contextReady ? '对话尚未加载完成，请刷新页面重试。' : conversation?.session?.can_edit === false ? '此策略已停用，会话仅可查看。' : '可继续讨论；发布需要你确认。';
    }
    function updateUrl() {
        const url = new URL(location.href);
        url.searchParams.set('workspace_id', config.workspace_id);
        url.searchParams.delete('new');
        if (sessionId) url.searchParams.set('session_id', sessionId); else url.searchParams.delete('session_id');
        if (strategy?.id) url.searchParams.set('strategy_id', strategy.id); else url.searchParams.delete('strategy_id');
        history.replaceState(null, '', url);
    }
    function ruleText(spec) {
        if (!spec) return '尚未选择已有策略。';
        const metrics = {frequency: '出现频次', omission: '遗漏期数', special_frequency: '特码频次'};
        const regions = (spec.regions || ['hk', 'macau']).map(region => region === 'hk' ? '香港' : '澳门').join('、');
        const lines = [regions];
        if (spec.kind === 'builtin_config') {
            if (spec.strategy_key === 'zodiac_rebound') lines.push(`生肖遗漏回补：统计 ${spec.parameters?.window ?? 20} 期，单双连开阈值 ${spec.parameters?.parity_streak_threshold ?? 5} 期，输出六肖与四肖。`);
            else lines.push(`内置策略副本：统计 ${spec.parameters?.window ?? 40} 期，号码候选池 ${spec.parameters?.pool ?? 16}，特码候选池 ${spec.parameters?.special_pool ?? 8}。`);
        } else if (spec.kind === 'number_selection') {
            lines.push(`统计 ${spec.window ?? 20} 期，筛选 ${spec.normal_count ?? 6} 个候选号码。`, `${metrics[spec.rank_metric || 'frequency']}优先${spec.rank_order === 'lowest' ? '较低' : '较高'}。`);
        } else if (spec.kind === 'zodiac_groups') {
            lines.push(`统计 ${spec.window ?? 20} 期。`);
            for (const group of spec.groups || []) lines.push(`${group.label || '生肖组合'}：${group.size} 肖，${metrics[group.metric || 'omission']}优先${group.order === 'lowest' ? '较低' : '较高'}。`);
        } else if (spec.kind === 'external_selection') lines.push(`澳门外部来源：${(spec.source_fields || []).map(field => field === 'numbers' ? '号码' : '生肖').join('、')}。`);
        return lines.join('\n');
    }
    function renderContext() {
        document.getElementById('assistantTarget').textContent = strategy ? `修改：${strategy.name}` : '新策略讨论';
        document.getElementById('assistantContext').textContent = strategy ? `已发布第 ${strategy.revision_no || 1} 版 · 对话中的草案不会自动改变当前策略` : '先讨论目标和规则，确定后再生成可回测草案。';
        document.getElementById('assistantRule').textContent = ruleText(strategy?.spec);
        document.getElementById('assistantNew').textContent = editTarget ? '为此策略新建对话' : '新对话';
        document.getElementById('assistantAttachSection').hidden = !!strategy;
        document.getElementById('assistantAttachStrategy').replaceChildren(text('option', '选择自己的策略'));
        document.getElementById('assistantAttachStrategy').firstElementChild.value = '';
        for (const item of attachableStrategies) {
            const option = text('option', item.name); option.value = item.id;
            document.getElementById('assistantAttachStrategy').append(option);
        }
    }
    function renderMessage(item) {
        const node = document.createElement('article'); node.className = 'assistant-message';
        node.dataset.role = item.role; if (item.id) node.dataset.messageId = item.id;
        const heading = document.createElement('div'); heading.className = 'assistant-message-heading';
        heading.append(text('strong', item.role === 'user' ? '你' : item.role === 'service' ? '服务提示' : '策略助手'));
        if (item.created_at) heading.append(text('time', new Date(item.created_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})));
        node.append(heading, text('div', item.content, 'assistant-message-content')); return node;
    }
    function renderMessages(rows, prepend = false) {
        const beforeHeight = transcript.scrollHeight;
        if (!prepend) messages.replaceChildren();
        const fragment = document.createDocumentFragment();
        for (const row of rows || []) fragment.append(renderMessage(row));
        if (prepend) { messages.prepend(fragment); transcript.scrollTop += transcript.scrollHeight - beforeHeight; }
        else { messages.append(fragment); transcript.scrollTop = transcript.scrollHeight; }
        document.getElementById('assistantWelcome').hidden = messages.childElementCount > 0;
    }
    function backtestText(backtest) {
        const summary = backtest?.summary || {};
        const lines = [`有效样本：${backtest?.sample_count || 0} 期`];
        const entries = [['主选号码', summary.special], ['候选覆盖', summary.normal_coverage], ...Object.entries(summary.groups || {}).map(([key,value]) => [key === 'six' ? '综合六肖' : key === 'four' ? '精选四肖' : key, value])];
        for (const [label,value] of entries) if (value) lines.push(`${label}：命中 ${value.hits}/${value.total}，命中率 ${value.hit_rate}%，随机基线 ${value.random_baseline}%。`);
        lines.push('历史统计不代表未来结果。'); return lines.join('\n');
    }
    function renderProposal(item) {
        proposalBox.replaceChildren(text('h3', '待确认草案'));
        if (!item) { proposalBox.append(text('p', '充分讨论后可请助手生成草案；这里会展示具体规则、改动和回测。')); return; }
        const statuses = {validated: '已校验 · 尚未发布', backtested: '已回测 · 等待确认', published: '已发布', superseded: '已被新草案替代', expired: '已过期', stale: '基准版本已变化，请重新讨论'};
        proposalBox.append(text('span', statuses[item.status] || '不可发布', 'assistant-proposal-status'), text('h3', item.proposal.name), text('p', item.proposal.change_summary));
        proposalBox.append(text('p', ruleText(item.proposal.spec), 'assistant-proposal-rules'));
        const details = document.createElement('details'); details.className = 'assistant-proposal-details';
        details.append(text('summary', '查看修改前后规则'), text('pre', `修改前\n${ruleText(item.diff?.before || strategy?.spec)}\n\n草案\n${ruleText(item.proposal.spec)}`));
        proposalBox.append(details);
        if (item.backtest) proposalBox.append(text('p', backtestText(item.backtest), 'assistant-proposal-rules'));
        if (!['validated', 'backtested'].includes(item.status) || conversation?.session?.can_edit === false) return;
        const regions = document.createElement('select'); regions.id = 'assistantBacktestRegion'; regions.setAttribute('aria-label', '回测地区');
        for (const region of item.proposal.spec.regions || ['hk', 'macau']) { const option = text('option', region === 'hk' ? '香港' : '澳门'); option.value = region; regions.append(option); }
        if (item.backtest?.region) regions.value = item.backtest.region;
        proposalBox.append(regions);
        const actions = document.createElement('div'); actions.className = 'assistant-proposal-actions';
        const operation = action => async event => {
            const expectedEpoch = epoch;
            const trigger = event.currentTarget;
            trigger.disabled = true; showError('');
            try { await action(); if (expectedEpoch === epoch) await refreshConversation(); }
            catch (error) { if (expectedEpoch === epoch) {showError(error.message); status.textContent = '草案操作未完成，请查看提示后重试。';} }
            finally { if (expectedEpoch === epoch) trigger.disabled = false; }
        };
        actions.append(button('回测草案', operation(async () => {
            status.textContent = '正在逐期回测草案…';
            await request(`${base}/strategy-proposals/${item.id}/backtests`, {method:'POST', body:JSON.stringify({region:regions.value})});
        })));
        if (item.status === 'backtested') actions.append(button('确认发布', operation(async () => {
            if (!window.confirm(`确认发布“${item.proposal.name}”？现有策略会切换到这一版本。`)) return;
            await request(`${base}/strategy-proposals/${item.id}/publish`, {method:'POST', body:JSON.stringify({confirm:true, expected_revision_id:item.base_revision_id})});
            status.textContent = '策略已发布，可继续讨论下一版。';
        }), true));
        proposalBox.append(actions);
    }
    function renderSessions() {
        sessionList.replaceChildren();
        for (const item of sessionItems) {
            const row = document.createElement('div'); row.className = 'assistant-session-row';
            const node = button('', () => openSession(item.id)); node.className = 'assistant-session';
            node.setAttribute('aria-current', String(item.id === sessionId));
            node.title = item.title;
            node.append(text('strong', item.title), text('small', item.strategy_name || '未关联策略'));
            const remove = button('删除', () => deleteConversation(item)); remove.className = 'assistant-session-delete';
            remove.setAttribute('aria-label', `删除对话：${item.title}`); remove.disabled = busy;
            row.append(node, remove); sessionList.append(row);
        }
        if (!sessionItems.length) sessionList.append(text('p', searchQuery ? '没有匹配的对话。' : '还没有对话，开始聊聊你的策略想法。'));
    }
    async function loadSessions(append = false) {
        const payload = await request(`${base}/strategy-assistant/sessions?page=${sessionPage}&limit=30&q=${encodeURIComponent(searchQuery)}`);
        sessionItems = append ? [...sessionItems, ...payload.items.filter(item => !sessionItems.some(old => old.id === item.id))] : payload.items;
        document.getElementById('assistantMoreSessions').hidden = !payload.has_more;
        renderSessions();
    }
    async function deleteConversation(item) {
        if (busy || !window.confirm(`删除对话“${item.title}”？对话、草案和相关回测将删除，已发布策略与预测记录会保留。`)) return;
        const expectedEpoch = epoch;
        setBusy(true); showError(''); document.getElementById('assistantThinking').hidden = true;
        try {
            await request(`${base}/strategy-assistant/sessions/${item.id}`, {method:'DELETE', body:JSON.stringify({confirm:true})});
            if (expectedEpoch !== epoch) return;
            if (item.id === sessionId) newConversation();
            sessionPage = 1; await loadSessions(); status.textContent = '对话已删除，已发布策略和运行记录保留。';
        } catch (error) { if (expectedEpoch === epoch) showError(error.message); }
        finally { if (expectedEpoch === epoch || sessionId === null) setBusy(false); }
    }
    async function refreshConversation() {
        if (!sessionId) return;
        const expectedEpoch = epoch, expectedId = sessionId;
        const payload = await request(`${base}/strategy-assistant/sessions/${expectedId}`);
        if (expectedEpoch !== epoch || expectedId !== sessionId) return;
        conversation = payload; strategy = payload.strategy; editTarget = strategy; contextReady = true; retryId = payload.retry_message_id;
        lastUserText = [...payload.messages].reverse().find(row => row.role === 'user')?.content || '';
        retryButton.hidden = !retryId;
        document.getElementById('assistantOlder').hidden = !payload.has_more_messages;
        renderContext(); renderMessages(payload.messages); renderProposal(payload.proposals?.[0]); updateUrl();
        if (!busy) setBusy(false);
        sessionPage = 1; await loadSessions();
    }
    async function openSession(id) {
        epoch += 1; sessionId = id; strategy = null; editTarget = null; contextReady = false;
        conversation = null; retryId = null; setBusy(true); showError(''); input.value = '';
        const expectedEpoch = epoch;
        renderContext(); renderMessages([]); renderProposal(null);
        status.textContent = '正在加载对话…'; document.getElementById('assistantThinking').hidden = true;
        app.classList.remove('show-history');
        try { await refreshConversation(); if (expectedEpoch === epoch) input.focus({preventScroll:true}); }
        catch (error) { if (expectedEpoch === epoch) showError(error.message); }
        finally { if (expectedEpoch === epoch) setBusy(false); }
    }
    function newConversation(clearTarget = false) {
        epoch += 1; sessionId = null; strategy = clearTarget ? null : editTarget; editTarget = strategy;
        contextReady = true; conversation = null; retryId = null; lastUserText = '';
        input.value = ''; showError(''); setBusy(false); renderContext(); renderMessages([]); renderProposal(null); renderSessions();
        retryButton.hidden = true; document.getElementById('assistantOlder').hidden = true;
        app.classList.remove('show-history'); updateUrl(); input.focus();
    }
    async function sendMessage(retry = false) {
        if (busy || !contextReady) return;
        const message = (retry ? lastUserText : input.value).trim();
        if (!message) return;
        const expectedEpoch = epoch;
        showError(''); setBusy(true);
        try {
            if (!sessionId) {
                const created = await request(`${base}/strategy-assistant/sessions`, {method:'POST', body:JSON.stringify({strategy_id:editTarget?.id || null, expected_strategy_id:editTarget?.id || null})});
                if (expectedEpoch !== epoch) return;
                sessionId = created.session_id; updateUrl();
            }
            const userRetryId = retryId && message === lastUserText ? retryId : null;
            if (!userRetryId) messages.append(renderMessage({role:'user',content:message}));
            document.getElementById('assistantWelcome').hidden = true;
            transcript.scrollTop = transcript.scrollHeight;
            const payload = await request(`${base}/strategy-assistant/sessions/${sessionId}/messages`, {method:'POST', body:JSON.stringify({message, expected_strategy_id:editTarget?.id || null, ...(userRetryId ? {retry_message_id:userRetryId} : {})})});
            if (expectedEpoch !== epoch) return;
            input.value = ''; retryId = null;
            await refreshConversation();
            if (payload.proposal_id) status.textContent = '新草案已展示，可继续讨论或回测。';
        } catch (error) {
            if (expectedEpoch !== epoch) return;
            showError(error.message);
            if (error.payload?.user_message_id) { retryId = error.payload.user_message_id; lastUserText = message; }
            try { await refreshConversation(); } catch (_) { /* Keep original actionable error. */ }
        } finally {
            if (expectedEpoch === epoch) { setBusy(false); retryButton.hidden = !retryId; input.focus({preventScroll:true}); }
        }
    }
    composer.addEventListener('submit', event => {event.preventDefault(); sendMessage();});
    retryButton.addEventListener('click', () => sendMessage(true));
    input.addEventListener('keydown', event => {if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {event.preventDefault(); sendMessage();}});
    document.getElementById('assistantNew').addEventListener('click', () => newConversation());
    document.getElementById('assistantNewStrategy').addEventListener('click', () => newConversation(true));
    document.getElementById('assistantSearch').addEventListener('submit', async event => {
        event.preventDefault(); searchQuery = document.getElementById('assistantSearchInput').value.trim(); sessionPage = 1;
        try { await loadSessions(); } catch (error) { showError(error.message); }
    });
    document.getElementById('assistantAttachForm').addEventListener('submit', async event => {
        event.preventDefault(); if (busy || !contextReady || strategy) return;
        const selected = attachableStrategies.find(item => item.id === document.getElementById('assistantAttachStrategy').value);
        if (!selected || !window.confirm(`将这段对话关联到“${selected.name}”？后续基于其当前规则讨论，之前的待确认草案会作废。`)) return;
        const expectedEpoch = epoch; setBusy(true); showError('');
        try {
            if (sessionId) {
                await request(`${base}/strategy-assistant/sessions/${sessionId}/strategy`, {method:'PATCH', body:JSON.stringify({strategy_id:selected.id,confirm:true})});
                if (expectedEpoch === epoch) await refreshConversation();
            } else {
                strategy = {id:selected.id,name:selected.name,revision_id:selected.active_revision_id,
                            revision_no:selected.revision?.revision_no,spec:selected.revision?.spec};
                editTarget = strategy; renderContext(); updateUrl();
            }
        } catch(error) { if (expectedEpoch === epoch) showError(error.message); }
        finally { if (expectedEpoch === epoch) setBusy(false); }
    });
    document.getElementById('assistantExample').addEventListener('click', event => {input.value = event.currentTarget.textContent; input.focus();});
    document.getElementById('assistantWorkspace').addEventListener('change', event => location.assign(`/user/strategy-assistant?workspace_id=${encodeURIComponent(event.target.value)}`));
    document.getElementById('assistantMoreSessions').addEventListener('click', async () => {sessionPage += 1; try {await loadSessions(true);} catch(error) {sessionPage -= 1;showError(error.message);}});
    document.getElementById('assistantOlder').addEventListener('click', async () => {
        const before = messages.firstElementChild?.dataset.messageId;
        if (!sessionId || !before) return;
        const expectedEpoch = epoch;
        try {
            const payload = await request(`${base}/strategy-assistant/sessions/${sessionId}?before_message_id=${before}`);
            if (expectedEpoch !== epoch) return;
            renderMessages(payload.messages, true); document.getElementById('assistantOlder').hidden = !payload.has_more_messages;
        } catch (error) {showError(error.message);}
    });
    for (const panel of ['history','inspector']) {
        const toggle = document.getElementById(panel === 'history' ? 'assistantHistoryToggle' : 'assistantInspectorToggle');
        toggle.addEventListener('click', () => {app.classList.toggle(`show-${panel}`);toggle.setAttribute('aria-expanded',String(app.classList.contains(`show-${panel}`)));});
        document.querySelector(`[data-close="${panel}"]`).addEventListener('click', () => {app.classList.remove(`show-${panel}`);toggle.setAttribute('aria-expanded','false');toggle.focus();});
    }
    document.addEventListener('keydown', event => {if(event.key === 'Escape') app.classList.remove('show-history','show-inspector');});
    async function initialize() {
        const expectedEpoch = epoch;
        contextReady = false; setBusy(true); status.textContent = '正在加载对话…';
        document.getElementById('assistantThinking').hidden = true;
        try {
            const available = await request(`${base}/strategies`);
            if (expectedEpoch !== epoch) return;
            attachableStrategies = (available.items || []).filter(item => item.status === 'active' && item.permissions?.edit);
            await loadSessions();
            if (expectedEpoch !== epoch) return;
            if (config.session_id) await openSession(config.session_id);
            else if (config.strategy_id) {
                const existing = config.new_session ? {items:[]} : await request(`${base}/strategy-assistant/sessions?strategy_id=${encodeURIComponent(config.strategy_id)}&limit=1`);
                if (expectedEpoch !== epoch) return;
                if (existing.items.length) await openSession(existing.items[0].id);
                else {
                    const payload = await request(`${base}/strategies/${encodeURIComponent(config.strategy_id)}`);
                    if (expectedEpoch !== epoch) return;
                    strategy = {id:payload.item.id,name:payload.item.name,revision_id:payload.item.active_revision_id,
                                revision_no:payload.item.revision?.revision_no,spec:payload.item.revision?.spec};
                    editTarget = strategy; contextReady = true; renderContext(); updateUrl(); input.focus({preventScroll:true});
                }
            } else { contextReady = true; renderContext();renderProposal(null); input.focus({preventScroll:true}); }
        } catch(error) { if (expectedEpoch === epoch) showError(error.message); }
        finally { if (expectedEpoch === epoch) setBusy(false); }
    }
    initialize();
})();
