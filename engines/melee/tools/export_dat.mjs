/** Export a standard costume using existing prepared source/target assets.
 * node tools/export_dat.mjs <character> <target> <output-directory>
 * Run prepare_native_fit_local.py and build_native_fit.py first.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {fitCharacter} from '../runtime/fitting/native-fit.mjs';
import {buildStandardCostume} from '../runtime/fitting/costume.mjs';

export async function exportDat(root,character,slug,output,{evidence=false}={}){
  if(!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(character)||!/^[a-z][a-z-]{0,31}$/.test(slug))throw Error('Invalid character or target');
  const base=path.join(root,'build/native-fit'),read=async file=>fs.readFile(path.join(base,file));
  const {default:createFit}=await import(new URL('../build/native-fit/fit.mjs',import.meta.url));
  const wasm=await read('fit.wasm');
  const module=await createFit({instantiateWasm(imports,receive){receive(new WebAssembly.Instance(new WebAssembly.Module(wasm),imports));return {};}});
  const source=JSON.parse(await read(`local/sources/${character}.json`)),rig=JSON.parse(await read(`local/targets/${slug}.json`));
  const target={...rig,...rig.layouts[0]};
  const fitted=fitCharacter(module,{...source,...target,origins:target.mode==='round'?source.roundOrigins:source.humanoidOrigins,normals:target.mode==='round'?source.normals:source.smoothNormals});
  const raw=await read(`local/targets/${slug}-0.dat`),texture=await read(`local/sources/${character}.rgba8`);
  const bytes=buildStandardCostume(raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength),source,target,fitted,texture,0);
  await fs.mkdir(output,{recursive:true});
  const filename=path.join(output,rig.slots[0].filename);await fs.writeFile(filename,new Uint8Array(bytes));
  if(evidence)await fs.writeFile(path.join(output,'expected.json'),JSON.stringify({
    positions:Array.from(fitted.positions),normals:Array.from(fitted.normals),joints:Array.from(fitted.joints),weights:Array.from(fitted.weights),triangles:source.triangles,uv:source.uv,
  }));
  return {filename,bytes:bytes.byteLength};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const [character,target,output]=process.argv.slice(2);
  if(!output)throw Error('Usage: node tools/export_dat.mjs <character> <target> <output-directory>');
  console.log(await exportDat(fileURLToPath(new URL('..',import.meta.url)),character,target,output));
}
