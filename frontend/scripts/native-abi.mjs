// Versioned fingerprint for the *installed native shell*. A web-only OTA release
// must match this fingerprint; changes to Android/iOS native code require
// incrementing NATIVE_SCHEMA even if package names stay the same.
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
const NATIVE_SCHEMA=1;
const pkg=JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8'));
const config=JSON.parse(readFileSync(new URL('../capacitor.config.json',import.meta.url),'utf8'));
const nativeDependencies=Object.fromEntries(
  Object.entries(pkg.dependencies||{}).filter(([name])=>
    name.startsWith('@capacitor/')||name.startsWith('@capgo/'))
    .sort(([a],[b])=>a.localeCompare(b))
);
const fingerprint=JSON.stringify({schema:NATIVE_SCHEMA,
  appId:config.appId,server:config.server,plugins:config.plugins,nativeDependencies});
process.stdout.write(createHash('sha256').update(fingerprint).digest('hex')+'\n');
