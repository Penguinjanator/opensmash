// WebHID fallback for Nintendo Switch Pro Controllers.
//
// Chrome on macOS pairs a Bluetooth Pro Controller but its built-in Nintendo
// driver stalls in the init handshake (it keeps re-sending "set player
// lights"), so the pad never reaches the Gamepad API. Safari is fine. Over
// WebHID the same Chrome can open the device and stream full input reports,
// so this script reads the controller directly and exposes it as a
// standard-mapping Gamepad through navigator.getGamepads() and synthetic
// gamepadconnected / gamepaddisconnected events. Everything downstream
// (controller-remap.js, port planning, the engine shell) is unchanged.
//
// Load BEFORE controller-remap.js: the remapper captures getGamepads once.
//
// The top document owns the HID connections; same-origin frames (the engine
// iframe) read the parent's pads instead of opening the device again.
//
// Pages can only see a HID device after the user picks it once in Chrome's
// chooser (openSmashHidPads.request(), needs a click). Chrome remembers the
// grant per origin, so later visits reconnect with no prompt.
(function installOpenSmashHidPads() {
  "use strict";

  if (window.openSmashHidPads) return;

  const NINTENDO = 0x057e;
  const PRO_CONTROLLER = 0x2009;
  const FILTERS = [{ vendorId: NINTENDO, productId: PRO_CONTROLLER }];
  // Past Chrome's fixed four slots so native pads never collide.
  const FIRST_INDEX = 8;
  const RUMBLE_NEUTRAL = [0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40];
  const SPI_STICK_CAL = 0x603d;
  const SPI_STICK_CAL_SIZE = 18;
  const SPI_USER_CAL = 0x8010;
  const SPI_USER_CAL_SIZE = 22;
  const INIT_RETRY_MS = 1000;
  const MAX_INIT_TRIES = 4;
  const NATIVE_ID = /Vendor:\s*057e\s+Product:\s*2009/i;

  const nativeGetGamepads = typeof navigator.getGamepads === "function"
    ? navigator.getGamepads.bind(navigator)
    : () => [];

  // Same-origin parent already runs the HID side: mirror it.
  let host = null;
  try {
    if (window.parent !== window && window.parent.openSmashHidPads) host = window.parent.openSmashHidPads;
  } catch { /* cross-origin parent */ }

  const listeners = new Set();
  const pads = []; // slot -> controller state (top document only)

  function emit(type, snapshot) {
    for (const fn of listeners) {
      try { fn(type, snapshot); } catch { /* listener from an unloaded frame */ }
    }
  }

  function dispatchPadEvent(type, snapshot) {
    const event = new Event(type);
    Object.defineProperty(event, "gamepad", { value: snapshot });
    window.dispatchEvent(event);
  }

  function nativeHasTwin() {
    try {
      return Array.from(nativeGetGamepads() || []).some((pad) => pad && pad.connected && NATIVE_ID.test(pad.id));
    } catch { return false; }
  }

  // ---- stick calibration --------------------------------------------------

  function unpack(b0, b1, b2) {
    return [((b1 << 8) & 0x0f00) | b0, (b2 << 4) | (b1 >> 4)];
  }
  function unpack9(bytes) {
    return [...unpack(bytes[0], bytes[1], bytes[2]), ...unpack(bytes[3], bytes[4], bytes[5]), ...unpack(bytes[6], bytes[7], bytes[8])];
  }
  const bogus = (values) => values.some((v) => v === 0 || v === 0xfff);
  // Left stick: max-above, center, min-below. Right stick: center, min, max.
  function leftCal(bytes) {
    const v = unpack9(bytes);
    return bogus(v) ? null : { x: { max: v[0], center: v[2], min: v[4] }, y: { max: v[1], center: v[3], min: v[5] } };
  }
  function rightCal(bytes) {
    const v = unpack9(bytes);
    return bogus(v) ? null : { x: { center: v[0], min: v[2], max: v[4] }, y: { center: v[1], min: v[3], max: v[5] } };
  }
  const DEFAULT_AXIS = Object.freeze({ center: 2048, min: 1400, max: 1400 });
  const DEFAULT_STICK = Object.freeze({ x: DEFAULT_AXIS, y: DEFAULT_AXIS });

  function axisValue(raw, cal) {
    const delta = raw - cal.center;
    const span = delta >= 0 ? cal.max : cal.min;
    if (!span) return 0;
    return Math.max(-1, Math.min(1, delta / span)) || 0; // no -0
  }

  // ---- controller ---------------------------------------------------------

  function makeButtons() {
    return Array.from({ length: 18 }, () => ({ pressed: false, touched: false, value: 0 }));
  }

  function createPad(device, slot) {
    const pad = {
      device,
      slot,
      counter: 0,
      reports: 0,
      fullReports: 0,
      tries: 0,
      timer: 0,
      cal: { left: null, right: null, userLeft: null, userRight: null },
      snapshot: null,
      connected: false,
    };
    pad.snapshot = {
      id: `${device.productName || "Pro Controller"} (STANDARD GAMEPAD Vendor: 057e Product: 2009) [WebHID]`,
      index: FIRST_INDEX + slot,
      connected: false,
      mapping: "standard",
      timestamp: performance.now(),
      axes: [0, 0, 0, 0],
      buttons: makeButtons(),
      vibrationActuator: null,
      webhid: true,
    };
    return pad;
  }

  async function subcommand(pad, id, args = []) {
    const data = new Uint8Array(48);
    data[0] = pad.counter;
    pad.counter = (pad.counter + 1) & 0x0f;
    data.set(RUMBLE_NEUTRAL, 1);
    data[9] = id;
    data.set(args, 10);
    await pad.device.sendReport(0x01, data);
  }

  function spiRead(pad, address, size) {
    return subcommand(pad, 0x10, [address & 0xff, (address >> 8) & 0xff, 0, 0, size]);
  }

  async function usbHandshake(pad) {
    // Wired Pro Controllers stay silent until told to talk HID over USB.
    for (const cmd of [0x02, 0x03, 0x02, 0x04]) {
      try { await pad.device.sendReport(0x80, new Uint8Array([cmd])); } catch { return; }
    }
  }

  async function initialize(pad) {
    clearTimeout(pad.timer);
    if (!pads[pad.slot] || pads[pad.slot] !== pad) return;
    pad.tries += 1;
    try {
      if (pad.tries > 1) await usbHandshake(pad);
      await subcommand(pad, 0x03, [0x30]); // full input report mode
      await subcommand(pad, 0x30, [1 << (pad.slot % 4)]); // player light
      if (!pad.cal.left) await spiRead(pad, SPI_STICK_CAL, SPI_STICK_CAL_SIZE);
      if (!pad.cal.userLeft && !pad.cal.userRight) await spiRead(pad, SPI_USER_CAL, SPI_USER_CAL_SIZE);
    } catch (error) {
      console.warn("webhid pad: init write failed", error);
    }
    pad.timer = setTimeout(() => {
      if (pad.fullReports === 0 && pad.tries < MAX_INIT_TRIES) initialize(pad);
    }, INIT_RETRY_MS);
  }

  function handleReply(pad, data) {
    // 0x21: 12 bytes of controller state, ack, subcommand id, reply.
    if (data[13] !== 0x10) return;
    const address = data[14] | (data[15] << 8);
    const bytes = data.subarray(19);
    if (address === SPI_STICK_CAL) {
      pad.cal.left = leftCal(bytes.subarray(0, 9));
      pad.cal.right = rightCal(bytes.subarray(9, 18));
    } else if (address === SPI_USER_CAL) {
      if (bytes[0] === 0xb2 && bytes[1] === 0xa1) pad.cal.userLeft = leftCal(bytes.subarray(2, 11));
      if (bytes[11] === 0xb2 && bytes[12] === 0xa1) pad.cal.userRight = rightCal(bytes.subarray(13, 22));
    }
  }

  function setButton(buttons, index, down) {
    const button = buttons[index];
    button.pressed = down;
    button.touched = down;
    button.value = down ? 1 : 0;
  }

  function updateState(pad, data) {
    const right = data[2], shared = data[3], left = data[4];
    const s = pad.snapshot;
    // Positional standard mapping, same as Chrome's native Nintendo driver:
    // bottom=B, right=A, left=Y, top=X.
    const buttons = makeButtons();
    setButton(buttons, 0, !!(right & 0x04));  // B
    setButton(buttons, 1, !!(right & 0x08));  // A
    setButton(buttons, 2, !!(right & 0x01));  // Y
    setButton(buttons, 3, !!(right & 0x02));  // X
    setButton(buttons, 4, !!(left & 0x40));   // L
    setButton(buttons, 5, !!(right & 0x40));  // R
    setButton(buttons, 6, !!(left & 0x80));   // ZL
    setButton(buttons, 7, !!(right & 0x80));  // ZR
    setButton(buttons, 8, !!(shared & 0x01)); // Minus
    setButton(buttons, 9, !!(shared & 0x02)); // Plus
    setButton(buttons, 10, !!(shared & 0x08)); // L-stick press
    setButton(buttons, 11, !!(shared & 0x04)); // R-stick press
    setButton(buttons, 12, !!(left & 0x02));  // D-up
    setButton(buttons, 13, !!(left & 0x01));  // D-down
    setButton(buttons, 14, !!(left & 0x08));  // D-left
    setButton(buttons, 15, !!(left & 0x04));  // D-right
    setButton(buttons, 16, !!(shared & 0x10)); // Home
    setButton(buttons, 17, !!(shared & 0x20)); // Capture

    const [lx, ly] = unpack(data[5], data[6], data[7]);
    const [rx, ry] = unpack(data[8], data[9], data[10]);
    const lc = pad.cal.userLeft || pad.cal.left || DEFAULT_STICK;
    const rc = pad.cal.userRight || pad.cal.right || DEFAULT_STICK;
    // Nintendo reports up as positive; the standard layout has up = -1.
    const axes = [axisValue(lx, lc.x), -axisValue(ly, lc.y) || 0, axisValue(rx, rc.x), -axisValue(ry, rc.y) || 0];

    // Fresh objects per report, like the browser's snapshots: callers that
    // hold an old snapshot keep what they saw.
    pad.snapshot = { ...s, axes, buttons, timestamp: performance.now() };
  }

  function onInputReport(pad, event) {
    const data = new Uint8Array(event.data.buffer, event.data.byteOffset, event.data.byteLength);
    pad.reports += 1;
    if (event.reportId === 0x30) {
      pad.fullReports += 1;
      updateState(pad, data);
    } else if (event.reportId === 0x21) {
      updateState(pad, data);
      handleReply(pad, data);
    } else {
      return; // 0x3F simple-HID mode: wait for the mode switch to land
    }
    if (!pad.connected) {
      pad.connected = true;
      pad.snapshot = { ...pad.snapshot, connected: true };
      console.log("webhid pad: connected", pad.snapshot.id, "index", pad.snapshot.index);
      emit("gamepadconnected", pad.snapshot);
    }
  }

  async function attach(device) {
    if (!device || device.vendorId !== NINTENDO || device.productId !== PRO_CONTROLLER) return false;
    if (pads.some((pad) => pad && pad.device === device)) return true;
    let slot = pads.findIndex((pad) => !pad);
    if (slot < 0) slot = pads.length;
    const pad = createPad(device, slot);
    pads[slot] = pad;
    try {
      if (!device.opened) await device.open();
    } catch (error) {
      console.warn("webhid pad: open failed", error);
      pads[slot] = null;
      return false;
    }
    device.addEventListener("inputreport", (event) => onInputReport(pad, event));
    initialize(pad);
    return true;
  }

  function detach(device) {
    const slot = pads.findIndex((pad) => pad && pad.device === device);
    if (slot < 0) return;
    const pad = pads[slot];
    clearTimeout(pad.timer);
    pads[slot] = null;
    if (pad.connected) {
      emit("gamepaddisconnected", { ...pad.snapshot, connected: false });
    }
  }

  function list() {
    return pads.filter((pad) => pad && pad.connected).map((pad) => pad.snapshot);
  }

  const supported = Boolean(navigator.hid) || Boolean(host?.supported);

  async function request() {
    if (host) return host.request();
    if (!navigator.hid) return false;
    const devices = await navigator.hid.requestDevice({ filters: FILTERS });
    let attached = false;
    for (const device of devices) attached = (await attach(device)) || attached;
    return attached;
  }

  function subscribe(fn) {
    if (host) return host.subscribe(fn);
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  function padsForGetGamepads() {
    const mine = host ? host.list() : list();
    // If Chrome's own driver ever works, prefer it over our copy.
    return mine.length && nativeHasTwin() ? [] : mine;
  }

  function getGamepads() {
    let result;
    try { result = Array.from(nativeGetGamepads() || []); } catch { result = []; }
    for (const pad of padsForGetGamepads()) result[pad.index] = pad;
    return result;
  }

  // Mirror connect/disconnect into this document as gamepad events.
  const unsubscribe = subscribe((type, snapshot) => {
    if (type === "gamepadconnected" && nativeHasTwin()) return;
    dispatchPadEvent(type, snapshot);
  });
  window.addEventListener("pagehide", () => { if (host) unsubscribe(); });

  if (!host && navigator.hid) {
    navigator.hid.addEventListener("connect", (event) => { attach(event.device); });
    navigator.hid.addEventListener("disconnect", (event) => { detach(event.device); });
    // Devices granted on an earlier visit reconnect without a prompt.
    navigator.hid.getDevices().then((devices) => devices.forEach(attach)).catch(() => {});
  }

  window.openSmashHidPads = Object.freeze({
    supported,
    list: () => (host ? host.list() : list()),
    request,
    subscribe,
  });

  try {
    Object.defineProperty(navigator, "getGamepads", { configurable: true, value: getGamepads });
  } catch {
    try { navigator.getGamepads = getGamepads; } catch { /* leave native */ }
  }
})();
