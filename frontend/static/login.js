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
  let holdSaid = false; // the message box shows "try again in N seconds" of the running pause

  function showMessage(text, options) {
    const settings = options || {};
    holdSaid = false;
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
    holdSaid = false;
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
   * that moment. The end is a moment, not a count of timer firings, so a PC that slept
   * or a tab in the background is released as soon as the pause is really over.
   *
   * Two clocks measure it: the time of day (it goes on while the PC sleeps) and the
   * browser's running clock (it does not jump when the time of day is set back). The
   * pause ends when either says so, and it never shows or lasts more than was asked.
   */
  let holdSeconds = 0;
  let holdWallEnd = 0;
  let holdMonoEnd = 0;

  function monoNow() {
    return typeof performance === 'object' && performance && typeof performance.now === 'function' ? performance.now() : null;
  }

  function holdLeft() {
    let left = Math.min(holdSeconds, Math.ceil((holdWallEnd - Date.now()) / 1000));
    const mono = monoNow();
    if (mono !== null) {
      left = Math.min(left, Math.ceil((holdMonoEnd - mono) / 1000));
    }
    return left;
  }

  function paintHold() {
    if (waitTimer === null) {
      return;
    }
    const left = holdLeft();
    if (left <= 0) {
      window.clearInterval(waitTimer);
      waitTimer = null;
      button.textContent = BUTTON_TEXT;
      button.disabled = busy;
      if (holdSaid) {
        // The box still says "try again in N seconds": that is over now.
        showMessage('Одоо дахин оролдож болно.');
      }
      return;
    }
    button.textContent = BUTTON_TEXT + ' · ' + left + ' сек';
  }

  function holdFor(seconds) {
    if (waitTimer !== null) {
      window.clearInterval(waitTimer);
    }
    const mono = monoNow();
    holdSeconds = seconds;
    holdWallEnd = Date.now() + seconds * 1000;
    holdMonoEnd = mono === null ? 0 : mono + seconds * 1000;
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
    paintHold(); // a pause that is over is ended here too, before any timer or focus event
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
        holdSaid = true;
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
