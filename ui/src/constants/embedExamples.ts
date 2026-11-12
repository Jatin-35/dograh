export const HEADLESS_CHAT_EXAMPLE = `let chatState = 'idle';

function withBotrixAIWidget(callback) {
  if (window.BotrixAIWidget) {
    callback(window.BotrixAIWidget);
    return;
  }

  const script = document.getElementById('botrixai-widget');
  if (!script) {
    console.error('BotrixAI embed script not found');
    return;
  }

  script.addEventListener('load', () => {
    if (window.BotrixAIWidget) callback(window.BotrixAIWidget);
  }, { once: true });
}

withBotrixAIWidget((widget) => {
  widget.onChatStateChange((state) => {
    chatState = state; // idle | starting | ready | waiting | ended | expired | error
  });

  widget.onMessage((text, turn) => {
    appendAgentBubble(text); // render however you want
  });

  document.getElementById('open-chat').addEventListener('click', () => {
    widget.startChat();
  });

  document.getElementById('send-btn').addEventListener('click', async () => {
    const input = document.getElementById('chat-input');
    appendVisitorBubble(input.value);
    const transcript = await widget.sendMessage(input.value);
    if (transcript !== null) input.value = '';
  });

  document.getElementById('end-chat')?.addEventListener('click', async () => {
    await widget.endChat();
  });
});`;
