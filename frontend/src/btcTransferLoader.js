// Keep any future Bitcoin cryptography out of MiniNex's startup bundle.
// This file is deliberately never imported by App.jsx or wallet UI.
// Invoked only on a future explicit user action, after Android smoke tests.
export async function loadBtcTransferEngine({enabled=false,loader}={}) {
  if(enabled!==true||typeof loader!=='function')
    return {ok:false,reason:'not_requested'};
  try {
    const engine=await loader();
    if(!engine||typeof engine.prepare!=='function'||
       typeof engine.sign!=='function'||
       typeof engine.broadcast!=='function')
      return {ok:false,reason:'engine_incompatible'};
    return {ok:true,engine};
  } catch {
    // Never let a dynamic crypto import failure blank the whole MiniNex UI.
    return {ok:false,reason:'engine_unavailable'};
  }
}
