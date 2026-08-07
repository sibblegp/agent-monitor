/** WebSocket client with automatic reconnect and backoff. */

import { wsUrl } from './platform.js';

export class Live {
  constructor(onMessage, onStatus) {
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.ws = null;
    this.retry = 0;
    this.closed = false;
    this.timer = null;
  }

  connect() {
    this.closed = false;
    try {
      this.ws = new WebSocket(wsUrl());
    } catch {
      this._scheduleRetry();
      return;
    }

    this.ws.addEventListener('open', () => {
      this.retry = 0;
      this.onStatus?.('open');
    });

    this.ws.addEventListener('message', (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.onMessage?.(data);
    });

    this.ws.addEventListener('close', () => {
      this.onStatus?.('closed');
      this._scheduleRetry();
    });

    this.ws.addEventListener('error', () => {
      // 'close' always follows, which is where the retry is scheduled.
      this.onStatus?.('error');
    });
  }

  _scheduleRetry() {
    if (this.closed) return;
    clearTimeout(this.timer);
    const delay = Math.min(600 * 2 ** this.retry, 8000);
    this.retry += 1;
    this.timer = setTimeout(() => this.connect(), delay);
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    this.ws?.close();
  }
}
