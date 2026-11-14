const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
function setup(){
  const elements=new Map();
  const element=()=>({textContent:'',scrollTop:0,scrollHeight:1000,clientHeight:100,children:[],append(...items){this.children.push(...items)},removeAttribute(name){delete this[name]}});
  const document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element};
  const source=fs.readFileSync(path.join(__dirname,'../control.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1].replace('__SESSION__','"test"').replace('refresh();find();setInterval(refresh,2500);','');
  const context=vm.createContext({document,setTimeout,clearTimeout,AbortController});
  vm.runInContext(source,context);
  return {context,elements};
}
test('log refresh preserves DOM and reading position; follows only at bottom',()=>{
  const {context,elements}=setup();
  vm.runInContext("renderLogs({node:'first'},'device-A')",context);
  const pre=elements.get('logs').children[1];
  pre.scrollTop=120;
  vm.runInContext("renderLogs({node:'second'},'device-A')",context);
  assert.equal(elements.get('logs').children[1],pre);
  assert.equal(pre.scrollTop,120);
  assert.match(elements.get('logs').children[0].textContent,/device-A/);
  pre.scrollTop=900;
  vm.runInContext("renderLogs({node:'third'},'device-A')",context);
  assert.equal(pre.scrollTop,1000);
});
test('failed refresh clears guard so later polling can recover',async()=>{
  const {context,elements}=setup();
  context.fetch=async()=>{throw Error('offline')};
  await vm.runInContext('refresh()',context);
  assert.equal(vm.runInContext('refreshing',context),false);
  assert.match(elements.get('error').textContent,/offline/);
});
test('workbench is linked while running and clears the revealed token when stopped',()=>{
  const {context,elements}=setup();
  vm.runInContext("renderWorkbench({local_url:'http://127.0.0.1:5173/',lan_url:'http://192.168.1.10:5173/'})",context);
  assert.equal(elements.get('workbenchBox').hidden,false);
  assert.equal(elements.get('workbench').href,'http://127.0.0.1:5173/#resources');
  assert.match(elements.get('workbenchLan').textContent,/192\.168\.1\.10:5173/);
  vm.runInContext("$('tokenValue').value='example'; $('tokenFallback').hidden=false; renderWorkbench()",context);
  assert.equal(elements.get('workbenchBox').hidden,true);
  assert.equal(elements.get('workbench').href,undefined);
  assert.equal(elements.get('tokenValue').value,'');
  assert.equal(elements.get('tokenFallback').hidden,true);
});
