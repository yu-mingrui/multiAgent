const $ = (selector) => document.querySelector(selector);
const messageList = $('#message-list');
const historyList = $('#history-list');
const historyCount = $('#history-count');
const queryInput = $('#query');
const sendButton = $('#send-button');
const threadLabel = $('#thread-label');
const endpointInput = $('#endpoint');
const userIdInput = $('#user-id');
const sessionIdInput = $('#session-id');
const profileAvatar = $('#profile-avatar');
const profileUser = $('#profile-user');
const profileSession = $('#profile-session');

let activeController = null;

// 释放发送状态，允许用户继续发起下一条消息。
function releaseSendState() {
  sendButton.disabled = false;
  $('#status-dot').style.background = '';
  activeController = null;
}

// 生成新的会话 ID，并同步更新顶部个人资料显示。
function setSessionId() {
  sessionIdInput.value = `session-${crypto.randomUUID().slice(0, 8)}`;
  updateProfile();
}

// 刷新用户和会话信息展示，保证页面头部信息与输入框保持一致。
function updateProfile() {
  const userId = userIdInput.value.trim() || '未设置用户';
  const sessionId = sessionIdInput.value.trim() || '未建立会话';
  profileUser.textContent = userId;
  profileSession.textContent = sessionId;
  profileAvatar.textContent = userId.charAt(0).toUpperCase();
}

// 在消息列表中追加一条消息气泡，并滚动到最新位置。
function appendMessage(role, text) {
  $('#welcome-block')?.remove();
  const message = document.createElement('article');
  message.className = `message ${role}`;
  message.innerHTML = `<div class="message-label">${role === 'user' ? 'YOU' : 'ASSISTANT'}</div><div class="message-bubble"></div>`;
  message.querySelector('.message-bubble').textContent = text;
  messageList.appendChild(message);
  messageList.scrollTop = messageList.scrollHeight;
  return message.querySelector('.message-bubble');
}

// 生成会话列表 API 的地址，兼容不同后端入口配置。
function sessionsEndpoint() {
  return new URL('/api/sessions', endpointInput.value.trim()).toString();
}

// 重新渲染历史会话列表，并标出当前正在查看的会话。
function renderHistory(sessions) {
  historyCount.textContent = `${sessions.length} 个会话`;
  if (!sessions.length) {
    historyList.innerHTML = '<div class="empty-history">暂无历史会话。</div>';
    return;
  }
  historyList.innerHTML = '';
  sessions.forEach((session) => {
    const item = document.createElement('div');
    item.className = `history-item${session.session_id === sessionIdInput.value ? ' active' : ''}`;
    item.dataset.sessionId = session.session_id;
    item.tabIndex = 0;
    item.innerHTML = '<div class="history-copy"><strong></strong><time></time><span></span></div><button class="history-delete" type="button" title="删除会话" aria-label="删除会话">×</button>';
    item.querySelector('strong').textContent = session.title || '未命名会话';
    item.querySelector('time').textContent = session.updated_at || '';
    item.querySelector('span').textContent = session.preview || '暂无回答';
    historyList.appendChild(item);
  });
}

async function refreshHistory() {
  const userId = userIdInput.value.trim();
  if (!userId) {
    renderHistory([]);
    return;
  }
  try {
    const response = await fetch(`${sessionsEndpoint()}?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) throw new Error(`历史会话接口返回 ${response.status}`);
    renderHistory((await response.json()).sessions || []);
  } catch (error) {
    historyList.innerHTML = '<div class="empty-history">历史会话暂时无法加载。</div>';
    historyCount.textContent = '加载失败';
  }
}

function renderConversation(messages) {
  messageList.innerHTML = '';
  messages.forEach((message) => appendMessage(message.role === 'assistant' ? 'assistant' : 'user', message.content));
  messageList.scrollTop = messageList.scrollHeight;
}

async function openHistory(sessionId) {
  if (activeController) return;
  try {
    const userId = userIdInput.value.trim();
    const response = await fetch(`${sessionsEndpoint()}/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) throw new Error(`历史会话详情返回 ${response.status}`);
    const session = await response.json();
    sessionIdInput.value = session.session_id;
    updateProfile();
    renderConversation(session.messages || []);
    threadLabel.textContent = '历史会话';
    renderHistory((await (await fetch(`${sessionsEndpoint()}?user_id=${encodeURIComponent(userId)}`)).json()).sessions || []);
  } catch (error) {
    threadLabel.textContent = '历史会话加载失败';
  }
}

async function deleteHistory(sessionId) {
  if (activeController || !confirm('确定删除这个会话吗？删除后无法恢复。')) return;
  try {
    const userId = userIdInput.value.trim();
    const response = await fetch(`${sessionsEndpoint()}/${encodeURIComponent(sessionId)}?user_id=${encodeURIComponent(userId)}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(`删除会话接口返回 ${response.status}`);
    if (sessionId === sessionIdInput.value) {
      setSessionId();
      messageList.innerHTML = '';
      threadLabel.textContent = '尚未发送消息';
    }
    await refreshHistory();
  } catch (error) {
    threadLabel.textContent = '会话删除失败';
  }
}

// 解析一段 SSE 数据块，并提取出 event 和 payload。
function parseSseBlock(block) {
  const lines = block.split(/\r?\n/);
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
  const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n');
  if (!event || !data) return null;
  try { return { event, data: JSON.parse(data) }; } catch { return null; }
}

// 将缓冲区中的多段 SSE 数据逐个分发给事件处理器。
function consumeSseBlocks(buffer, onEvent) {
  const blocks = buffer.split(/\r?\n\r?\n/);
  const remainder = blocks.pop() || '';
  for (const block of blocks) {
    const parsed = parseSseBlock(block);
    if (parsed) onEvent(parsed);
  }
  return remainder;
}

// 发送用户消息并接收后端 SSE 流，实时展示回答增量内容。
async function sendMessage(query) {
  appendMessage('user', query);
  const assistantBubble = appendMessage('assistant', '正在连接工作流...');
  sendButton.disabled = true;
  $('#status-dot').style.background = '#e98c6e';
  activeController = new AbortController();

  try {
    const response = await fetch(endpointInput.value.trim(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-User-Id': userIdInput.value.trim(), 'X-Session-Id': sessionIdInput.value.trim() },
      body: JSON.stringify({ query, user_id: userIdInput.value.trim() || null, session_id: sessionIdInput.value.trim() || null }),
      signal: activeController.signal,
    });
    if (!response.ok || !response.body) throw new Error(`接口返回 ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let answerText = '';
    let renderedText = '';
    let typingTimer = null;
    let receivedAnswer = false;

    const renderAnswer = () => {
      if (renderedText.length >= answerText.length) {
        typingTimer = null;
        return;
      }
      renderedText += answerText[renderedText.length];
      assistantBubble.textContent = renderedText;
      messageList.scrollTop = messageList.scrollHeight;
      typingTimer = setTimeout(renderAnswer, 18);
    };

    const handleEvent = (parsed) => {
      if (typeof parsed.data?.delta === 'string') {
        receivedAnswer = true;
        console.log(parsed.data.delta);
        answerText += parsed.data.delta;
        if (!typingTimer) renderAnswer();
      }
      if (parsed.event === 'done') {
        threadLabel.textContent = '回答完成';
        releaseSendState();
      }
      if (parsed.event === 'error') {
        assistantBubble.textContent = parsed.data.message || '模型调用失败';
        releaseSendState();
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      buffer = consumeSseBlocks(buffer, handleEvent);
      if (done) break;
    }
    if (buffer.trim()) {
      const parsed = parseSseBlock(buffer);
      if (parsed) handleEvent(parsed);
    }
    if (!receivedAnswer && assistantBubble.textContent === '正在连接工作流...') assistantBubble.textContent = '工作流已完成，但没有返回文本内容。';
  } catch (error) {
    if (error.name !== 'AbortError') assistantBubble.textContent = `连接失败：${error.message}`;
  } finally {
    releaseSendState();
  }
  await refreshHistory();
}

$('#composer').addEventListener('submit', (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query || activeController) return;
  queryInput.value = '';
  queryInput.style.height = 'auto';
  sendMessage(query);
});

queryInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); }
});
queryInput.addEventListener('input', () => { queryInput.style.height = 'auto'; queryInput.style.height = `${Math.min(queryInput.scrollHeight, 130)}px`; });
userIdInput.addEventListener('input', updateProfile);
sessionIdInput.addEventListener('input', updateProfile);

document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => { queryInput.value = button.dataset.prompt; queryInput.focus(); }));
$('#new-chat').addEventListener('click', () => { setSessionId(); $('#clear-chat').click(); queryInput.focus(); });
historyList.addEventListener('click', (event) => {
  if (event.target.closest('.history-delete')) {
    deleteHistory(event.target.closest('.history-item').dataset.sessionId);
    return;
  }
  const item = event.target.closest('.history-item');
  if (item) openHistory(item.dataset.sessionId);
});
historyList.addEventListener('keydown', (event) => {
  if ((event.key === 'Enter' || event.key === ' ') && event.target.closest('.history-item')) {
    event.preventDefault();
    openHistory(event.target.closest('.history-item').dataset.sessionId);
  }
});
userIdInput.addEventListener('change', refreshHistory);
$('#clear-chat').addEventListener('click', () => { messageList.innerHTML = ''; threadLabel.textContent = '尚未发送消息'; });
setSessionId();
refreshHistory();
