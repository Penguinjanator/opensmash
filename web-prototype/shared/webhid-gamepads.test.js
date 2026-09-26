import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/webhid-gamepads.js", import.meta.url), "utf8");
const remapSource = await readFile(new URL("../public/controller-remap.js", import.meta.url), "utf8");

function fakeDevice() {
  const listeners = [];
  return {
    vendorId: 0x057e,
    productId: 0x2009,
    productName: "Pro Controller",
    opened: false,
    sent: [],
    async open() { this.opened = true; },
    async sendReport(id, data) { this.sent.push([id, Array.from(data)]); },
    addEventListener(type, fn) { if (type === "inputreport") listeners.push(fn); },
    input(reportId, bytes) {
      const data = new Uint8Array(63);
      data.set(bytes);
      for (const fn of listeners) fn({ reportId, data: new DataView(data.buffer) });
    },
  };
}

function packStick(x, y) {
  return [x & 0xff, ((x >> 8) & 0x0f) | ((y & 0x0f) << 4), (y >> 4) & 0xff];
}

async function harness({ granted = [], native = [] } = {}) {
  const windowListeners = {};
  const events = [];
  const window = {
    addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
    dispatchEvent(event) { events.push(event); (windowListeners[event.type] || []).forEach((fn) => fn(event)); },
  };
  window.parent = window;
  const navigator = {
    getGamepads: () => native,
    hid: {
      addEventListener() {},
      getDevices: async () => granted,
      requestDevice: async () => granted,
    },
  };
  class Event { constructor(type) { this.type = type; } }
  const context = {
    window, navigator, Event, performance: { now: () => 1 }, console: { log() {}, warn() {} },
    setTimeout: () => 0, clearTimeout() {}, Uint8Array, Promise, Object, Array, Math, Boolean,
    Set, Number, JSON, String, Proxy, Reflect, localStorage: { getItem: () => null, setItem() {} },
  };
  vm.runInNewContext(source, context);
  vm.runInNewContext(remapSource, context);
  await new Promise((resolve) => setImmediate(resolve));
  return { window, navigator, events };
}

test("a granted Pro Controller becomes a standard gamepad after its first full report", async () => {
  const device = fakeDevice();
  const { navigator, events } = await harness({ granted: [device] });
  assert.equal(device.opened, true);
  // Init asks for full report mode (subcommand 0x03, arg 0x30).
  assert.ok(device.sent.some(([id, data]) => id === 0x01 && data[9] === 0x03 && data[10] === 0x30));
  assert.equal(navigator.getGamepads().filter(Boolean).length, 0);

  // A held (right byte 0x08), D-left (left byte 0x08), left stick hard right.
  device.input(0x30, [0, 0x90, 0x08, 0x00, 0x08, ...packStick(2048 + 1400, 2048), ...packStick(2048, 2048)]);
  const pads = navigator.getGamepads().filter(Boolean);
  assert.equal(pads.length, 1);
  const [pad] = pads;
  assert.equal(pad.mapping, "standard");
  assert.equal(pad.index, 8);
  assert.equal(pad.buttons[1].pressed, true); // A is the right face button
  assert.equal(pad.buttons[0].pressed, false);
  assert.equal(pad.buttons[14].pressed, true);
  assert.equal(pad.axes[0], 1);
  assert.equal(pad.axes[1], 0);
  assert.equal(events.filter((e) => e.type === "gamepadconnected").length, 1);
  assert.equal(events[0].gamepad.index, 8);
});

test("stick up maps to negative Y and factory calibration is applied", async () => {
  const device = fakeDevice();
  const { navigator } = await harness({ granted: [device] });
  // SPI reply for 0x603d: left = max-above 1000/1000, center 2000/2000, min-below 1000/1000.
  const pack2 = (a, b) => [a & 0xff, ((a >> 8) & 0x0f) | ((b & 0x0f) << 4), (b >> 4) & 0xff];
  const left = [...pack2(1000, 1000), ...pack2(2000, 2000), ...pack2(1000, 1000)];
  const right = [...pack2(2000, 2000), ...pack2(1000, 1000), ...pack2(1000, 1000)];
  const reply = new Array(19).fill(0);
  reply[12] = 0x90; reply[13] = 0x10; reply[14] = 0x3d; reply[15] = 0x60; reply[18] = 18;
  device.input(0x21, [...reply, ...left, ...right]);
  device.input(0x30, [0, 0x90, 0, 0, 0, ...packStick(2000, 2500), ...packStick(2000, 2000)]);
  const [pad] = navigator.getGamepads().filter(Boolean);
  assert.equal(pad.axes[0], 0);
  assert.equal(pad.axes[1], -0.5);
});

test("the WebHID copy hides when the browser exposes the same controller natively", async () => {
  const device = fakeDevice();
  const native = [{ id: "Pro Controller (STANDARD GAMEPAD Vendor: 057e Product: 2009)", index: 0, connected: true, axes: [], buttons: [] }];
  const { navigator } = await harness({ granted: [device], native });
  device.input(0x30, [0, 0x90, 0, 0, 0, ...packStick(2048, 2048), ...packStick(2048, 2048)]);
  const pads = navigator.getGamepads().filter(Boolean);
  assert.equal(pads.length, 1);
  assert.equal(pads[0].index, 0);
});
