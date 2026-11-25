// Run: node tests/strategy_workspace_e2e.cjs (requires playwright or playwright-core).
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {createRequire} = require('node:module');
const load = process.env.PLAYWRIGHT_MODULE_PATH
  ? createRequire(path.join(process.env.PLAYWRIGHT_MODULE_PATH, 'package.json'))
  : require;
const {chromium} = load('playwright-core');

(async () => {
  const server = spawn(process.env.PYTHON || '.venv/bin/python', ['tests/strategy_browser_server.py'], {
    stdio: ['ignore', 'pipe', 'pipe'], cwd: path.resolve(__dirname, '..'),
  });
  let output = '';
  server.stderr.on('data', chunk => { output += chunk; });
  let browser;
  let page;
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'mark-six-browser-'));
  try {
    const fixture = await new Promise((resolve, reject) => {
      let pending = '';
      const timeout = setTimeout(() => reject(new Error(`fixture startup timeout: ${output}`)), 30000);
      server.once('exit', code => reject(new Error(`fixture exited ${code}: ${output}`)));
      server.stdout.on('data', chunk => {
        pending += chunk;
        for (const line of pending.split('\n')) {
          if (line.startsWith('BROWSER_FIXTURE ')) {
            clearTimeout(timeout); resolve(JSON.parse(line.slice(16))); return;
          }
        }
      });
    });
    browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
      args: ['--no-sandbox']});
    const context = await browser.newContext({viewport: {width: 1280, height: 900}, acceptDownloads: true});
    const origin = `http://127.0.0.1:${fixture.port}`;
    await context.addCookies([{name: 'session', value: fixture.cookie, url: origin}]);
    await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', async response => {
      if (response.status() >= 400 && response.url().startsWith(origin)) output += `\nHTTP ${response.status()} ${response.url()} ${await response.text().catch(() => '')}`;
    });
    page.on('dialog', async dialog => dialog.type() === 'prompt' ? dialog.accept('20') : dialog.accept());
    // Admin pages use the real dark surface for both OS theme preferences.
    await page.goto(`${origin}/admin/users`);
    for (const scheme of ['dark', 'light']) {
      await page.emulateMedia({colorScheme: scheme});
      await page.locator(`tr[data-user-id="${fixture.user_id}"]`).getByRole('button', {name: '设置分组', exact: true}).click();
      const modal = page.locator('#strategyWorkspaceModal');
      await modal.locator('.workspace-option-info strong').first().waitFor();
      const contrasts = await modal.evaluate(element => {
        const color = text => text.match(/[\d.]+/g).map(Number);
        const composite = (foreground, background) => {
          const alpha = foreground[3] ?? 1;
          return foreground.slice(0, 3).map((value, index) => value * alpha + background[index] * (1-alpha));
        };
        const background = node => {
          const parent = node.parentElement ? background(node.parentElement) : [255,255,255];
          return composite(color(getComputedStyle(node).backgroundColor), parent);
        };
        const luminance = rgb => rgb.slice(0,3).map(value => value/255).map(value => value <= .04045 ? value/12.92 : ((value+.055)/1.055)**2.4).reduce((sum,value,index) => sum+value*[.2126,.7152,.0722][index],0);
        return [...element.querySelectorAll('h3,.workspace-option-info strong')].map(node => {
          const bg = background(node); const fg = composite(color(getComputedStyle(node).color), bg);
          const values = [luminance(bg),luminance(fg)].sort((a,b) => b-a);
          return {label: node.textContent, ratio: (values[0]+.05)/(values[1]+.05)};
        });
      });
      assert(contrasts.every(value => value.ratio >= 4.5), `${scheme}: ${JSON.stringify(contrasts)}`);
      console.log(`PASS contrast ${scheme}: ${contrasts.map(value => value.ratio.toFixed(2)).join(', ')}`);
      await modal.getByRole('button', {name: '关闭', exact: true}).click();
    }
    await page.getByRole('link', {name: '工作区管理 / GitHub 备份', exact: true}).click();
    const workspace = page.locator(`#workspace-${fixture.workspaces[0]}`);
    await workspace.getByLabel('工作区名称', {exact: true}).fill('策略研究工作区');
    await workspace.getByRole('button', {name: '保存名称', exact: true}).click();
    await page.getByText('已保存“策略研究工作区”。', {exact: true}).waitFor();
    await workspace.getByRole('link', {name: '配置 GitHub 备份', exact: true}).click();
    await page.locator('#strategyBackupSection').waitFor();
    assert((await page.locator('#strategyBackupSection').innerText()).includes('所有已发布策略共用此备份设置'));
    await page.getByRole('button', {name: '修改工作区名称', exact: true}).click();
    await page.locator('#strategyEditDialog').getByLabel('工作区名称', {exact: true}).fill('策略工作区');
    await page.locator('#strategyEditDialog').getByRole('button', {name: '保存', exact: true}).click();
    await page.locator('#strategyWorkspaceName').getByText('策略工作区', {exact: true}).waitFor();
    const builtin = page.locator('#strategyCatalogList .strategy-card-row').filter({has: page.getByText('生肖遗漏回补', {exact: true})});
    await builtin.getByRole('button', {name: '复制为自定义策略', exact: true}).click();
    const editor = () => page.locator('#strategyEditDialog');
    await editor().getByLabel('策略名称', {exact: true}).fill('生肖研究一号');
    await editor().getByRole('button', {name: '保存', exact: true}).click();
    const zodiac = name => page.locator('#customStrategyList .strategy-card-row').filter({has: page.getByText(name, {exact: true})});
    await zodiac('生肖研究一号').waitFor();
    await zodiac('生肖研究一号').getByRole('button', {name: '编辑策略', exact: true}).click();
    await editor().getByLabel('策略名称', {exact: true}).fill('生肖研究二号');
    await editor().getByLabel('统计窗口（期）', {exact: true}).fill('10');
    await editor().getByLabel('单双连开阈值（期）', {exact: true}).fill('6');
    await editor().getByRole('button', {name: '保存', exact: true}).click();
    await zodiac('生肖研究二号').getByText(/统计 10 期.*单双连开阈值 6 期/).waitFor();
    await zodiac('生肖研究二号').getByRole('button', {name: 'AI 修改', exact: true}).click();
    await page.waitForURL('**/user/strategy-assistant?**');
    await page.getByRole('heading', {name:'修改：生肖研究二号', exact:true}).waitFor();
    const chatInput = page.getByLabel('你的想法', {exact:true});
    const strategyId = new URL(page.url()).searchParams.get('strategy_id');
    const chatBase = `${origin}/api/v2/workspaces/${fixture.workspaces[0]}/strategy-assistant/sessions`;
    assert(strategyId, 'AI modify must preserve its explicit strategy target');
    await page.getByRole('button', {name:'为此策略新建对话', exact:true}).click();
    await page.getByRole('heading', {name:'修改：生肖研究二号', exact:true}).waitFor();
    const hotMessage = '我想改成40期的热门筛选，算是和现在遗漏回补相反的预测方向，可以吗';
    const createdRequest = page.waitForRequest(request => request.method() === 'POST' && request.url() === chatBase);
    const messageRequest = page.waitForRequest(request => request.method() === 'POST' && request.url().startsWith(chatBase) && request.url().endsWith('/messages'));
    await chatInput.fill(hotMessage);
    await page.getByRole('button', {name:'发送', exact:true}).click();
    assert.equal((await createdRequest).postDataJSON().expected_strategy_id, strategyId);
    assert.equal((await messageRequest).postDataJSON().expected_strategy_id, strategyId);
    await page.locator('.assistant-message[data-role="assistant"]').getByText(/当前修改“生肖研究二号”：保留综合六肖和精选四肖/).waitFor();
    const hotSessionId = new URL(page.url()).searchParams.get('session_id');
    const hotSession = await (await context.request.get(`${chatBase}/${hotSessionId}`)).json();
    assert.equal(hotSession.strategy.id, strategyId);
    assert.equal(hotSession.strategy.spec.strategy_key, 'zodiac_rebound');
    await page.getByRole('button', {name:'为此策略新建对话', exact:true}).click();
    assert.equal(new URL(page.url()).searchParams.get('strategy_id'), strategyId);
    const shortTermMessage = '我想做短期版的，统计改成 10 期，预防近期开过的肖会在近期继续出现';
    await chatInput.fill(shortTermMessage);
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.locator('.assistant-message[data-role="assistant"]').getByText(/你说的预防重复/).waitFor();
    assert.equal(await page.locator('.assistant-message[data-role="user"]').getByText(shortTermMessage, {exact:true}).count(), 1);
    assert.equal(await page.getByRole('button', {name:'确认发布', exact:true}).count(), 0);
    const initialChatUrl = page.url();
    await chatInput.fill('先保留 10 期，暂不排除近期生肖，生成草案');
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.locator('#assistantProposal').getByText('已校验 · 尚未发布', {exact:true}).waitFor();
    await page.reload();
    await page.locator('.assistant-message[data-role="assistant"]').getByText(/已整理 10 期版草案/).waitFor();
    await page.locator('#assistantProposal').getByText(/统计 10 期/).first().waitFor();
    assert.equal(page.url(), initialChatUrl);
    await chatInput.fill('先讨论，不要修改：为什么仍可能重复？');
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.locator('.assistant-message[data-role="assistant"]').getByText(/保留已经讨论的 10 期窗口/).waitFor();
    await page.screenshot({path:'/tmp/mark-six-strategy-chat-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await page.reload();
    await page.locator('.assistant-message[data-role="assistant"]').getByText(/保留已经讨论的 10 期窗口/).waitFor();
    await page.waitForFunction(() => {
      const main = document.querySelector('.main-content').getBoundingClientRect();
      const input = document.querySelector('#assistantInput').getBoundingClientRect();
      const sidebar = document.querySelector('.sidebar').getBoundingClientRect();
      return main.width >= innerWidth - 2 && input.width >= innerWidth - 80 && sidebar.right <= 1;
    });
    const inputRect = await chatInput.boundingBox();
    assert(inputRect && inputRect.y >= 0 && inputRect.y + inputRect.height <= 844, 'mobile composer must remain visible');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    await page.screenshot({path:'/tmp/mark-six-strategy-chat-mobile.png'});
    await page.setViewportSize({width:1280,height:900});
    await chatInput.fill('测试接口限制');
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.locator('#assistantError').getByText(/不支持此网页后端调用/).waitFor();
    assert.equal(await chatInput.inputValue(), '测试接口限制');
    await page.getByRole('button', {name:'重试上一条', exact:true}).click();
    await page.locator('.assistant-message[data-role="assistant"]').getByText('连接恢复了，可以继续讨论策略。', {exact:true}).waitFor();
    assert.equal(await page.locator('.assistant-message[data-role="user"]').getByText('测试接口限制', {exact:true}).count(), 1);
    // Pending proposal and prior discussion remain available after the failed turn.
    await page.locator('#assistantBacktestRegion').selectOption('macau');
    await page.getByRole('button', {name:'回测草案', exact:true}).click();
    await page.getByRole('button', {name:'确认发布', exact:true}).waitFor();
    await page.reload();
    await page.getByRole('button', {name:'确认发布', exact:true}).waitFor();
    await page.getByRole('button', {name:'确认发布', exact:true}).click();
    await page.locator('#assistantProposal').getByText('已发布', {exact:true}).waitFor();
    // Search/delete the published discussion while preserving the strategy.
    const publishedSessionId = new URL(page.url()).searchParams.get('session_id');
    await page.getByLabel('查找对话', {exact:true}).fill('短期版');
    await page.getByRole('button', {name:'搜索', exact:true}).click();
    await page.waitForFunction(() => document.querySelectorAll('.assistant-session-row').length === 1);
    const publishedRow = page.locator('.assistant-session-row').filter({has:page.getByText(shortTermMessage, {exact:true})});
    const deletedResponse = page.waitForResponse(response => response.request().method() === 'DELETE' && response.url() === `${chatBase}/${publishedSessionId}`);
    await publishedRow.getByRole('button', {name:`删除对话：${shortTermMessage}`, exact:true}).click();
    assert.equal((await deletedResponse).status(), 200);
    await page.getByText('没有匹配的对话。', {exact:true}).waitFor();
    assert.equal((await context.request.get(`${chatBase}/${publishedSessionId}`)).status(), 404);
    const retainedStrategy = await (await context.request.get(`${origin}/api/v2/workspaces/${fixture.workspaces[0]}/strategies/${strategyId}`)).json();
    assert.equal(retainedStrategy.item.status, 'active');
    assert.equal(retainedStrategy.item.revision.spec.parameters.window, 10);
    assert.equal(new URL(page.url()).searchParams.get('session_id'), null);
    assert.equal(new URL(page.url()).searchParams.get('strategy_id'), strategyId);
    await page.getByLabel('查找对话', {exact:true}).fill('');
    await page.getByRole('button', {name:'搜索', exact:true}).click();
    // A deliberately unbound discussion can be associated without losing history.
    await page.getByRole('button', {name:'讨论新策略', exact:true}).click();
    await page.getByRole('heading', {name:'新策略讨论', exact:true}).waitFor();
    const unboundMessage = '之前忘了关联策略，先保留这段讨论';
    await chatInput.fill(unboundMessage);
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.locator('.assistant-message[data-role="assistant"]').getByText('这段讨论尚未关联策略，可以先选择要继续修改的已有策略。', {exact:true}).waitFor();
    const attachSessionId = new URL(page.url()).searchParams.get('session_id');
    const unboundSession = await (await context.request.get(`${chatBase}/${attachSessionId}`)).json();
    assert.equal(unboundSession.strategy, null);
    await page.getByLabel('要修改的策略', {exact:true}).selectOption(strategyId);
    await page.getByRole('button', {name:'关联所选策略', exact:true}).click();
    await page.getByRole('heading', {name:'修改：生肖研究二号', exact:true}).waitFor();
    const attachedSession = await (await context.request.get(`${chatBase}/${attachSessionId}`)).json();
    assert.equal(attachedSession.strategy.id, strategyId);
    assert(attachedSession.messages.some(message => message.role === 'user' && message.content === unboundMessage));
    await chatInput.fill(hotMessage);
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.locator('.assistant-message[data-role="assistant"]').getByText(/当前修改“生肖研究二号”：保留综合六肖和精选四肖/).waitFor();
    // A failed initial target lookup must never enable an unbound send.
    const targetApi = `${origin}/api/v2/workspaces/${fixture.workspaces[0]}/strategies/${strategyId}`;
    await page.route(targetApi, route => route.fulfill({status:503, contentType:'application/json',
      body:JSON.stringify({success:false, message:'策略信息暂时无法加载'})}));
    await page.goto(`${origin}/user/strategy-assistant?workspace_id=${fixture.workspaces[0]}&strategy_id=${strategyId}&new=1`);
    await page.getByRole('alert').getByText('策略信息暂时无法加载', {exact:true}).waitFor();
    assert.equal(await page.getByRole('button', {name:'发送', exact:true}).isDisabled(), true);
    assert.equal(await chatInput.evaluate(node => node.readOnly), true);
    assert((await page.locator('#assistantStatus').innerText()).includes('刷新页面重试'));
    await page.unroute(targetApi);
    await page.reload();
    await page.getByRole('heading', {name:'修改：生肖研究二号', exact:true}).waitFor();
    assert.equal(await page.getByRole('button', {name:'发送', exact:true}).isEnabled(), true);
    assert.equal(new URL(page.url()).searchParams.get('strategy_id'), strategyId);
    await page.goto(`${origin}/user/strategy-workspace?workspace_id=${fixture.workspaces[0]}`);
    await page.locator('#strategyRunRegion').selectOption('macau');
    await page.locator('#strategyCreateForm').getByLabel('策略名称', {exact: true}).fill('E2E rule');
    await page.getByRole('button', {name: '保存策略', exact: true}).click();
    const rule = () => page.locator('#customStrategyList .strategy-card-row').filter({has: page.getByText('E2E rule', {exact: true})});
    await rule().waitFor();
    await rule().getByRole('button', {name: '运行', exact: true}).click();
    await rule().getByText(/候选号码：/).waitFor();
    await rule().getByRole('button', {name: '自动订阅当前地区', exact: true}).click();
    await rule().getByRole('button', {name: '取消订阅', exact: true}).click();
    await page.waitForFunction(() => ![...document.querySelectorAll('#customStrategyList button')].some(button => button.textContent === '取消订阅'));
    await rule().getByRole('button', {name: '分享到工作区', exact: true}).click();
    await rule().getByRole('button', {name: '撤回分享', exact: true}).waitFor();
    await rule().getByRole('button', {name: '编辑策略', exact: true}).click();
    await editor().getByLabel('统计窗口（期）', {exact: true}).fill('20');
    await editor().getByRole('button', {name: '保存', exact: true}).click();
    await rule().getByText(/第 2 版/).waitFor();
    await rule().getByRole('button', {name: '运行记录', exact: true}).click();
    await rule().getByText(/第 2026326 期/).waitFor();
    const downloadPromise = page.waitForEvent('download');
    await rule().getByRole('button', {name: '导出 JSON', exact: true}).click();
    const download = await downloadPromise;
    const bundlePath = path.join(temporary, 'strategy.json');
    await download.saveAs(bundlePath);
    await page.locator('#strategyBundleFile').setInputFiles(bundlePath);
    await page.getByRole('button', {name: '预览本地备份', exact: true}).click();
    await page.getByRole('button', {name: '确认导入本地草稿', exact: true}).click();
    await page.getByRole('button', {name: '发布草稿', exact: true}).click();
    await page.waitForFunction(() => ![...document.querySelectorAll('#customStrategyList button')].some(button => button.textContent === '发布草稿'));
    await page.locator('#strategyWorkspaceSelect').selectOption(String(fixture.workspaces[1]));
    await page.getByText('此工作区还没有自定义策略。').waitFor();
    await page.getByRole('link', {name:'新策略对话', exact:true}).click();
    await page.getByLabel('你的想法', {exact:true}).fill('按近20期频率生成规则');
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await page.getByRole('button', {name:'回测草案', exact:true}).waitFor();
    assert.equal(await page.getByRole('button', {name:'确认发布', exact:true}).count(), 0);
    await page.locator('#assistantBacktestRegion').selectOption('macau');
    await page.getByRole('button', {name:'回测草案', exact:true}).click();
    await page.getByRole('button', {name:'确认发布', exact:true}).click();
    await page.locator('#assistantProposal').getByText('已发布', {exact:true}).waitFor();
    await page.getByRole('link', {name:'返回策略工作区', exact:true}).click();
    const assistant = page.locator('#customStrategyList .strategy-card-row').filter({has: page.getByText('E2E assistant', {exact: true})});
    await assistant.waitFor();
    await assistant.getByRole('button', {name: '归档', exact: true}).click();
    await page.getByText('此工作区还没有自定义策略。').waitFor();
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#strategyWorkspaceSelect').selectOption(String(fixture.workspaces[0]));
    await page.locator('#customStrategyList .strategy-card-row').first().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, 'mobile must not overflow horizontally');
    await zodiac('生肖研究二号').getByRole('button', {name: '编辑策略', exact: true}).click();
    assert.equal(await editor().evaluate(node => node.scrollWidth > node.clientWidth + 2), false, 'mobile editor must not overflow horizontally');
    await editor().getByRole('button', {name: '取消', exact: true}).click();
    assert.deepEqual(errors, [], 'browser must not report JavaScript errors');
    console.log('PASS: admin dark/light contrast, workspace rename, backup entry, zodiac clone/name/parameters, bound new conversations/40-period context, standalone AI reply/multiturn/history/retry/publish, search/delete with published strategy retained, unbound-history association, failed target lookup locks composer/reload recovers, create, run, subscribe/cancel, share, edit, history, export, local restore/publish, workspace switch, AI confirmation, archive, mobile layout');
  } catch (error) {
    console.error(output.slice(-5000));
    if (page) console.error(await page.locator('body').innerText().catch(() => 'page unavailable'));
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await fs.rm(temporary, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
