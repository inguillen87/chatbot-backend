import {chromium,expect} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react-swc';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const backend=new URL(process.env.GUIDE_PAIR_API);
assert.equal(backend.hostname,'127.0.0.1');
const actors=JSON.parse(process.env.GUIDE_PAIR_ACTORS),ports=JSON.parse(process.env.GUIDE_PAIR_PORTS);
const paths=JSON.parse(process.env.GUIDE_PAIR_PATHS),folder=process.env.GUIDE_PAIR_EVIDENCE;
delete process.env.GUIDE_PAIR_ACTORS;
const servers=[],origins={};let browser;const results=[];
const controlPath='/api/admin/tenants/acceptance-a/conversation-guide-control';
const guidePath='/api/admin/tenants/acceptance-a/conversation-guide';
const activationPath='/api/v2/tenants/acceptance-a/activation/channels';
const entry='/.vercel/guide-pair-runtime/index.html';
const read=async(context,origin,route)=>{
 const response=await context.request.get(origin+route,{maxRedirects:0});
 assert.equal(response.status(),200,route);return response.json();
};
try{
 await mkdir(folder,{recursive:true});
 for(const [index,label] of ['operator','owner','foreign'].entries()){
  // Only an isolated identity exchange is simulated; no business response is mocked.
  const proxy={target:backend.origin,changeOrigin:true,headers:{Authorization:'Bearer '+actors[label].token}};
  const server=await createServer({configFile:false,envDir:'.vercel/guide-pair-runtime/empty-env',plugins:[react()],
   resolve:{alias:[{find:'@',replacement:path.resolve('src')}]},optimizeDeps:{entries:[entry.slice(1)]},
   cacheDir:'.vercel/guide-pair-cache-'+label,server:{host:'127.0.0.1',port:ports[index],strictPort:true,proxy:{'/api':proxy,'/auth':proxy}},logLevel:'error'});
  await server.listen();servers.push(server);origins[label]=`http://127.0.0.1:${ports[index]}`;
 }
 browser=await chromium.launch({headless:true});
 for(const [width,height,dark] of [[1440,1000,false],[390,844,true],[320,740,false]]){
  const operator=await browser.newContext({viewport:{width,height},reducedMotion:'reduce'});
  const owner=await browser.newContext({viewport:{width,height},reducedMotion:'reduce'});
  const foreign=await browser.newContext();const errors=[],uiWrites=[],blocked=[];
  for(const context of [operator,owner,foreign]){
   if(dark)await context.addInitScript(()=>{
    const apply=()=>document.documentElement.classList.add('dark');
    if(document.documentElement)apply();
    document.addEventListener('DOMContentLoaded',apply,{once:true});
   });
   await context.route('**/*',route=>{
    if(!Object.values(origins).includes(new URL(route.request().url()).origin)){blocked.push('external');return route.abort();}
    return route.continue();
   });
  }
  const page=await operator.newPage(),reader=await owner.newPage();
  page.on('pageerror',e=>errors.push(e.message));reader.on('pageerror',e=>errors.push(e.message));
  page.on('request',request=>{if(request.method()==='PUT')uiWrites.push({path:new URL(request.url()).pathname,body:request.postDataJSON()});});
  try{
   const initial=await read(operator,origins.operator,controlPath),ui=initial.ui;
   assert.equal(initial.state.enabled,false);assert.equal(initial.installed_guide.node_count,29);
   await page.goto(origins.operator+entry);await reader.goto(origins.owner+entry);
   if(dark){await page.evaluate(()=>document.documentElement.classList.add('dark'));await reader.evaluate(()=>document.documentElement.classList.add('dark'));}
   await expect(page.getByTestId('paired-identity')).toHaveAttribute('data-user-id',String(actors.operator.id));
   await expect(reader.getByTestId('paired-identity')).toHaveAttribute('data-user-id',String(actors.owner.id));
   const control=page.getByTestId('private-guide-control');await expect(control).toBeVisible();
   const ownerActivation=await read(owner,origins.owner,activationPath);
   assert.equal(ownerActivation.conversation_guide_control,null);
   assert.equal(ownerActivation.organization_setup.conversation_guide,null);
   await expect(reader.getByTestId('private-guide-control')).toHaveCount(0);
   await expect(reader.locator('.private-guide>summary')).toHaveCount(0);
   await control.getByRole('button',{name:ui.open,exact:true}).click();
   await control.getByRole('button',{name:ui.enable,exact:true}).click();
   await expect(control.getByRole('button',{name:ui.confirm,exact:true})).toBeDisabled();
   await control.getByRole('button',{name:ui.cancel,exact:true}).click();assert.equal(uiWrites.length,0);
   await control.getByRole('button',{name:ui.enable,exact:true}).click();
   await control.getByRole('checkbox',{name:ui.acknowledgement,exact:true}).check();
   const axe=await new AxeBuilder({page}).include('[data-testid="private-guide-control"]').withTags(['wcag2a','wcag2aa']).analyze();
   const severe=axe.violations.filter(issue=>['serious','critical'].includes(issue.impact));assert.deepEqual(severe.map(issue=>issue.id),[]);
   if(dark)await expect(page.locator('html')).toHaveClass(/\bdark\b/);
   await page.screenshot({path:`${folder}/review-${width}.png`,fullPage:true});
   await control.getByRole('button',{name:ui.confirm,exact:true}).evaluate(button=>{
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}));button.dispatchEvent(new MouseEvent('click',{bubbles:true}));
   });
   await expect(page.locator('.private-guide>summary')).toBeVisible();assert.equal(uiWrites.length,1);
   const enabled=await read(operator,origins.operator,controlPath);
   assert.equal(enabled.state.enabled,true);assert.equal(enabled.state.version,initial.state.version+1);
   const headers={'Content-Type':'application/json','X-Chatboc-Guide-Control':'1'};
   assert.equal((await operator.request.put(origins.operator+controlPath,{headers,data:uiWrites[0].body,maxRedirects:0})).status(),412);
   assert.equal((await foreign.request.get(origins.foreign+guidePath,{maxRedirects:0})).status(),403);
   assert.equal((await foreign.request.get(origins.foreign+controlPath,{maxRedirects:0})).status(),403);
   assert.equal((await owner.request.put(origins.owner+controlPath,{headers,data:uiWrites[0].body,maxRedirects:0})).status(),403);
   assert.equal((await operator.request.put(origins.operator+controlPath,{headers:{...headers,Origin:'https://not-trusted.example.invalid'},data:uiWrites[0].body,maxRedirects:0})).status(),403);
   await reader.reload();
   const summary=reader.locator('.private-guide>summary');await expect(summary).toBeVisible();
   const traverse=async action=>{
    const [response]=await Promise.all([reader.waitForResponse(r=>new URL(r.url()).pathname===guidePath&&r.request().method()==='GET'),action()]);
    assert.equal(response.status(),200);
    assert.match(response.headers()['cache-control'],/no-store/);const node=await response.json();
    assert.equal(node.tenant.id,initial.tenant.id);assert.equal(node.guide_sha256,initial.installed_guide.guide_sha256);
    assert.equal(node.source.sha256,initial.installed_guide.source.sha256);
    await expect(reader.getByRole('heading',{name:node.menu.title,exact:true})).toBeVisible();return node;
   };
   let current=await traverse(()=>summary.click());const visited=new Set([current.menu.id]);
   const targets=width===1440?Object.keys(paths):['start','main','documentation'];
   for(const target of targets){
    current=await traverse(()=>reader.locator('.private-guide-toolbar').getByRole('button',{name:current.ui.start,exact:true}).click());
    for(const code of paths[target]){
     const choice=current.menu.actions.find(item=>item.code===code);assert.ok(choice,'Published option missing');
     current=await traverse(()=>reader.locator('.private-guide-options').getByRole('button',{name:choice.label,exact:true}).click());
    }
    assert.equal(current.menu.id,target);visited.add(target);
   }
   if(width===1440)assert.equal(visited.size,29);
   if(dark)await expect(reader.locator('html')).toHaveClass(/\bdark\b/);
   const dimensions=await reader.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
   assert.ok(dimensions.scroll<=dimensions.width+1);await reader.screenshot({path:`${folder}/reader-${width}.png`,fullPage:true});
   const stored=await reader.evaluate(()=>JSON.stringify([Object.values(localStorage),Object.values(sessionStorage)]));
   assert.ok(!stored.includes(current.menu.text),'Guide content must not persist in browser storage');
   await page.reload();await expect(control).toBeVisible();
   await control.getByRole('button',{name:ui.open,exact:true}).click();
   await control.getByRole('button',{name:ui.disable,exact:true}).click();
   await control.getByRole('checkbox',{name:ui.acknowledgement,exact:true}).check();
   await control.getByRole('button',{name:ui.confirm,exact:true}).click();
   await expect(page.locator('.private-guide>summary')).toHaveCount(0);
   const disabled=await read(operator,origins.operator,controlPath);
   assert.equal(disabled.state.enabled,false);assert.equal(disabled.state.version,initial.state.version+2);
   const [denied]=await Promise.all([reader.waitForResponse(r=>new URL(r.url()).pathname===guidePath),
    reader.locator('.private-guide-toolbar').getByRole('button',{name:current.ui.back_to_menu,exact:true}).click()]);
   assert.equal(denied.status(),404);await expect(reader.getByTestId('private-guide-node')).toHaveCount(0);
   await expect(reader.getByRole('alert')).toHaveText(current.ui.error);
   const [reloaded]=await Promise.all([reader.waitForResponse(r=>new URL(r.url()).pathname===activationPath),reader.reload()]);
   assert.equal(reloaded.status(),200);assert.equal((await reloaded.json()).organization_setup.conversation_guide,null);
   await expect(reader.getByTestId('paired-identity')).toBeVisible();
   await expect(reader.locator('.private-guide>summary')).toHaveCount(0);
   assert.equal(uiWrites.length,2);assert.deepEqual(errors,[]);
   results.push({width,height,dark,passed:true,realFlask:true,realApiFetch:true,
    guideNodesVisited:visited.size,sourceHashVerified:true,uiPuts:2,
    replayRejected:true,foreignTenantDenied:true,tenantAdminWriteDenied:true,
    untrustedOriginDenied:true,existingSessionRevoked:true,fullPageReloadVerified:true,darkModeVerifiedAfterReload:dark,
    seriousAccessibilityViolations:severe.length,externalRequestsBlocked:blocked.length});
  }catch(error){
   results.push({width,height,dark,passed:false,reason:error.message,errors,uiPutCount:uiWrites.length});
   await writeFile(`${folder}/failure.json`,JSON.stringify(results.at(-1),null,2));
   await page.screenshot({path:`${folder}/failure-operator-${width}.png`,fullPage:true}).catch(()=>{});
   await reader.screenshot({path:`${folder}/failure-owner-${width}.png`,fullPage:true}).catch(()=>{});
  }finally{await operator.close();await owner.close();await foreign.close();}
  if(!results.at(-1).passed)break;
 }
 const report={frontendSha:process.env.GUIDE_PAIR_FRONTEND_SHA,fullSPA:false,
  productionAccountsUsed:false,identityExchangeSynthetic:true,httpPayloadsMocked:false,
  originalGuideSource:true,results};
 await writeFile(`${folder}/browser-results.json`,JSON.stringify(report,null,2));
 console.log(JSON.stringify(report));
 assert.ok(results.length===3&&results.every(row=>row.passed),'Paired guide regression failed');
}finally{await browser?.close();for(const server of servers)await server.close();}
