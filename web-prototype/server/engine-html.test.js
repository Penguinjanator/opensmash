import assert from "node:assert/strict";
import test from "node:test";
import { withControllerRemap } from "./engine-html.js";

test("injects the controller remapper before engine scripts run", () => {
  const html = "<html><head><title>Engine</title></head><body><script>boot()</script></body></html>";
  const result = withControllerRemap(html);
  assert.match(result, /<script src="\/controller-remap\.js"><\/script>\s*<\/head>/);
  assert.ok(result.indexOf("controller-remap.js") < result.indexOf("boot()"));
});

test("does not inject the remapper twice", () => {
  const once = withControllerRemap("<head></head>");
  assert.equal(withControllerRemap(once), once);
});

test("loads the WebHID pad adapter before the remapper", () => {
  const result = withControllerRemap("<head></head><body><script>boot()</script></body>");
  assert.ok(result.indexOf("webhid-gamepads.js") < result.indexOf("controller-remap.js"));
  const legacy = withControllerRemap('<head><script src="/controller-remap.js"></script></head>');
  assert.ok(legacy.indexOf("webhid-gamepads.js") >= 0 && legacy.indexOf("webhid-gamepads.js") < legacy.indexOf("controller-remap.js"));
  assert.equal(legacy.match(/controller-remap\.js/g).length, 1);
});

test('N64 keyboard adapter installs before engine scripts and is idempotent', async () => {
  const {withN64Keyboard}=await import('./engine-html.js');
  const html='<head></head><body><script>boot()</script></body>';
  const result=withN64Keyboard(withControllerRemap(html));
  assert.ok(result.indexOf('openSmashN64Keyboard.installEngine()')<result.indexOf('boot()'));
  assert.equal(withN64Keyboard(result),result);
});
