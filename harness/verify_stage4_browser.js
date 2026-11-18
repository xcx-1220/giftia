// Playwright CLI run-code snippet. verify_stage4.py supplies isolated fixture values.
async page => {
  const fixture = "__ISLA_FIXTURE__";
  const waitText = (text, root = page) => root.getByText(text, { exact: false }).first().waitFor({ timeout: 12000 });
  const send = async text => {
    await page.getByRole('textbox', { name: '消息', exact: true }).fill(text);
    await page.getByRole('button', { name: '发送', exact: true }).click();
  };
  const login = async token => {
    await page.getByLabel('会话凭据').fill(token);
    await page.getByRole('button', { name: '进入', exact: true }).click();
  };

  await login(fixture.owner);
  await waitText('搭档 · 隔离会话');
  await send('网页验收的普通消息。');
  await waitText('普通消息已收到。');
  await waitText('参考资料找到了');

  const uploadUrl = '**/api/web/attachments';
  await page.route(uploadUrl, route => route.abort());
  await page.locator('#pdf').setInputFiles(fixture.pdf);
  await waitText('上传中断，请重试。');
  await page.unroute(uploadUrl);
  await page.getByRole('button', { name: '重试上传' }).click();
  await waitText('PDF · 2 页', page.locator('#selection'));
  const files = page.getByRole('complementary', { name: '会话资料' });
  await page.getByRole('button', { name: '资料', exact: true }).click();
  await waitText('synthetic-reference.pdf', files);
  await files.getByRole('button', { name: '关闭', exact: true }).click();
  await files.waitFor({ state: 'hidden' });

  await send('网页上两页分别有哪些标签？');
  const turn = page.locator('.turn').filter({ hasText: '网页上两页分别有哪些标签？' });
  await waitText('ORBIT-41', turn);
  await turn.getByRole('button', { name: '第 1 页', exact: true }).waitFor();
  await turn.getByRole('button', { name: '第 2 页', exact: true }).click();
  await waitText('Desk label: LANTERN-72', page.getByRole('dialog'));
  await page.screenshot({ path: fixture.root + '/browser-source.png' });
  await page.getByRole('button', { name: '关闭来源' }).click();

  const receipt = page.waitForResponse(response => response.url().endsWith('/api/web/turns')
    && response.request().method() === 'POST' && response.status() === 202);
  await send('网页追问归还时间呢？');
  await receipt;
  await page.context().setOffline(true);
  await waitText('连接中断，正在重连');
  await page.context().setOffline(false);
  await waitText('网页追问的截止时间是 16:30');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitText('网页追问的截止时间是 16:30');
  await waitText('synthetic-reference.pdf · 已就绪');

  await send('网页核对无效页码和重试。');
  await waitText('回答的页码引用无效，请重试。');
  await page.getByRole('button', { name: '重试回复' }).click();
  await waitText('有效引用是归还截止时间 16:30');
  const box = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }));
  if (box.viewport > 460 || box.content !== box.viewport) throw new Error('Unexpected phone viewport or horizontal overflow');
  await page.screenshot({ path: fixture.root + '/browser-phone.png' });
  const mobile = page.viewportSize();
  await page.setViewportSize({ width: 1280, height: 800 });
  await waitText('synthetic-reference.pdf', files);
  await page.screenshot({ path: fixture.root + '/browser-desktop.png' });
  await page.setViewportSize(mobile);

  await page.getByRole('button', { name: '退出', exact: true }).click();
  await login(fixture.guest);
  await waitText('访客 · 隔离会话');
  await page.getByRole('heading', { name: '和艾拉聊聊' }).waitFor();
  if (await page.locator('.file,.turn').count()) throw new Error('Guest received private document or history');
  await page.screenshot({ path: fixture.root + '/browser-visitor.png' });
  return { status: 'PASS', viewport: box, ordinary: true, offlineRecovery: true, guestIsolation: true };
}
