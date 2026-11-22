/*
 * TradePilot AI: the login page.
 * Sends the password to POST /api/login as JSON and goes to the dashboard on success.
 * Messages are built with textContent only.
 */
(function () {
  'use strict';

  const form = document.getElementById('login-form');
  const input = document.getElementById('password');
  const button = document.getElementById('login-btn');
  const box = document.getElementById('login-msg');
  if (!form || !input || !button || !box) {
    return;
  }

  const BUTTON_TEXT = 'Нэвтрэх';
  const SET_PASSWORD_COMMAND = 'python -m backend.scripts.set_password';
  const TIMEOUT_MS = 15000;

  let busy = false;
  let waitTimer = null;

  function showMessage(text, options) {
    const settings = options || {};
    box.replaceChildren();
    box.className = settings.wait ? 'msg is-wait' : 'msg';
    const line = document.createElement('span');
    line.textContent = text;
    box.appendChild(line);
    if (settings.command) {
      const code = document.createElement('code');
      code.textContent = settings.command;
      box.appendChild(code);
    }
    box.hidden = false;
  }

  function clearMessage() {
    box.hidden = true;
    box.replaceChildren();
  }

  function setBusy(on) {
    busy = on;
    button.disabled = on || waitTimer !== null;
    if (waitTimer === null) {
      button.textContent = on ? 'Шалгаж байна' : BUTTON_TEXT;
    }
  }

  /*
   * After too many wrong passwords the server asks for a pause: hold the button until
   * that moment. The end is a time of day, not a count of timer firings, so a PC that
   * slept or a tab in the background is released as soon as the pause is really over.
   */
  let holdUntil = 0;

  function paintHold() {
    if (waitTimer === null) {
      return;
    }
    const left = Math.ceil((holdUntil - Date.now()) / 1000);
    if (left <= 0) {
      window.clearInterval(waitTimer);
      waitTimer = null;
      button.textContent = BUTTON_TEXT;
      button.disabled = busy;
      return;
    }
    button.textContent = BUTTON_TEXT + ' · ' + left + ' сек';
  }

  function holdFor(seconds) {
    if (waitTimer !== null) {
      window.clearInterval(waitTimer);
    }
    holdUntil = Date.now() + seconds * 1000;
    button.disabled = true;
    waitTimer = window.setInterval(paintHold, 1000);
    paintHold();
  }

  document.addEventListener('visibilitychange', paintHold);
  window.addEventListener('focus', paintHold);

  function retryAfterSeconds(response) {
    const raw = response.headers.get('Retry-After');
    const seconds = raw === null ? NaN : parseInt(raw, 10);
    return Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds, 3600) : null;
  }

  async function submit() {
    if (busy || waitTimer !== null) {
      return;
    }
    const password = input.value;
    if (!password) {
      input.setAttribute('aria-invalid', 'true');
      showMessage('Нууц үгээ оруулна уу.');
      input.focus();
      return;
    }

    clearMessage();
    input.removeAttribute('aria-invalid');
    setBusy(true);

    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? window.setTimeout(function () { controller.abort(); }, TIMEOUT_MS) : null;
    let response;
    try {
      response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ password: password }),
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller ? controller.signal : undefined,
      });
    } catch (error) {
      setBusy(false);
      showMessage('Сервертэй холбогдож чадсангүй.');
      return;
    } finally {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    }

    if (response.status === 200) {
      button.textContent = 'Нэвтэрлээ';
      window.location.replace('/');
      return;
    }

    setBusy(false);
    if (response.status === 401) {
      input.value = '';
      input.setAttribute('aria-invalid', 'true');
      showMessage('Нууц үг буруу.');
      input.focus();
    } else if (response.status === 429) {
      const seconds = retryAfterSeconds(response);
      if (seconds === null) {
        showMessage('Олон удаа буруу оруулсан. Түр хүлээгээд дахин оролдоно уу.', { wait: true });
      } else {
        showMessage('Олон удаа буруу оруулсан. ' + seconds + ' секундын дараа дахин оролдоно уу.', { wait: true });
        holdFor(seconds);
      }
    } else if (response.status === 503) {
      showMessage('Сервер дээр нууц үг тохируулаагүй байна.', { command: SET_PASSWORD_COMMAND });
    } else if (response.status === 422) {
      showMessage('Нууц үгээ оруулна уу.');
      input.focus();
    } else {
      showMessage('Серверийн алдаа гарлаа (код ' + response.status + '). Дараа дахин оролдоно уу.');
    }
  }

  // Sent here by the dashboard when its session ran out: say why the login page is back.
  if (/[?&]expired=1(&|$)/.test(window.location.search)) {
    document.title = 'Нэвтрэх хугацаа дууссан · TradePilot AI';
    showMessage('Нэвтрэх хугацаа дууссан тул мэдэгдэл зогссон. Дахин нэвтэрнэ үү.', { wait: true });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    submit();
  });

  input.addEventListener('input', function () {
    input.removeAttribute('aria-invalid');
  });
})();
