// Strict public GitHub Releases allowlist. No remote scripts are evaluated here.
const TAG = /^mininex-ota-v(0\.8\.([1-9]\d*))$/;
const SHA = /^[a-f0-9]{64}$/i;
const COMMIT = /^[a-f0-9]{40}$/i;
const MAX_ZIP_BYTES = 40 * 1024 * 1024;

const readField = (body,key) => {
  if(typeof body!=='string')return null;
  const match=body.match(new RegExp('(?:^|\\n)'+key+':\\s*([a-f0-9]+)\\s*(?:\\n|$)','i'));
  return match?.[1]?.toLowerCase()||null;
};

export function otaBuildNumber(value) {
  if(typeof value!=='string')return null;
  const m=value.match(/^0\.8\.([1-9]\d*)$/);
  return m ? Number(m[1]) : null;
}

export function parseOtaRelease(release,nativeAbi) {
  if(!release||typeof release!=='object'||release.draft!==false||
     release.prerelease!==false||typeof nativeAbi!=='string'||!SHA.test(nativeAbi))return null;
  const tag=typeof release.tag_name==='string'?release.tag_name:'';
  const match=tag.match(TAG);
  if(!match)return null;
  const version=match[1];
  const buildNumber=otaBuildNumber(version);
  const date=Date.parse(release.published_at);
  const sha=release.target_commitish;
  if(!Number.isSafeInteger(buildNumber)||!Number.isFinite(date)||
     typeof sha!=='string'||!COMMIT.test(sha))return null;
  if(readField(release.body,'native-abi')!==nativeAbi.toLowerCase())return null;
  if(readField(release.body,'source-commit')!==sha.toLowerCase())return null;
  const checksum=readField(release.body,'sha256');
  if(!checksum||!SHA.test(checksum))return null;
  const assets=Array.isArray(release.assets)?release.assets:[];
  const exactUrl='https://github.com/Quantumpepe/MiniNex/releases/download/'+tag+'/mininex-web.zip';
  const asset=assets.find(x=>x?.name==='mininex-web.zip'&&
    x.browser_download_url===exactUrl&&Number.isSafeInteger(x.size)&&x.size>1000&&x.size<=MAX_ZIP_BYTES);
  if(!asset)return null;
  // The checksum is also computed by GitHub for the immutable uploaded release asset.
  if(typeof asset.digest==='string'&&asset.digest!==''&&asset.digest.toLowerCase()!=='sha256:'+checksum)return null;
  return {version,buildNumber,url:exactUrl,checksum,sourceSha:sha.toLowerCase(),publishedAt:date};
}

export function shouldInstallOta(candidate,{currentVersion='builtin',pendingVersion=null,nativeBuildSha='',nativeBuildAt=''}={}) {
  if(!candidate||!Number.isSafeInteger(candidate.buildNumber))return false;
  const pending=otaBuildNumber(pendingVersion);
  if(pending!==null&&pending>=candidate.buildNumber)return false;
  if(currentVersion==='builtin'){
    const built=Date.parse(nativeBuildAt);
    return Number.isFinite(built)&&candidate.publishedAt>built&&
      typeof nativeBuildSha==='string'&&COMMIT.test(nativeBuildSha)&&
      candidate.sourceSha!==nativeBuildSha.toLowerCase();
  }
  const active=otaBuildNumber(currentVersion);
  return active!==null&&candidate.buildNumber>active;
}
