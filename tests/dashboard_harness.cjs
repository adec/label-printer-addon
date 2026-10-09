const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const lang = process.argv[2];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'', value:'', clientWidth:640,
    classList:{add(){},remove(){}}, addEventListener(){}, querySelectorAll(){return []}, style:{}});
  return elements.get(id);
}
const document = {documentElement:{lang}, baseURI:'http://example.test/ingress/',
  querySelector: element, getElementById: id => element('#'+id), addEventListener(){}, hidden:false};
const location = {href:'http://example.test/ingress/?lang='+lang, origin:'http://example.test', port:'', host:'example.test'};
const context = {document,location,URL,Intl,console,setTimeout(){},clearTimeout(){},setInterval(){},addEventListener(){},
  localStorage:{getItem(){return null},setItem(){}}, fetch:async()=>({json:async()=>({printers:[],series:[],journal:[]})})};
context.window=context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(0,'utf8'),context);
vm.runInContext(`
S={printers:[{name:'brother',kind:'brother',title:'Brother',connected:true,default:true,dpi:300,
  native_px:[696,1181],roll:{tracked:true,left:120,capacity:200,pct:60,used:80},accepts:['png']}],
  attention:[],today:{ok:3,fail:1,per_printer:{brother:3}},series:[],journal:[{ok:true,printer:'brother',summary:'legacy'}],journal_max:100};
render();
if (statusOf({connected:true,name:'brother'},[{printer:'brother',reason:'media_out'}]).t !== tr('Labels op')) throw Error('alert status');
`,context);
assert.ok(element('#printers').innerHTML.includes(lang==='en' ? 'Loaded label' : 'Geladen label'));
assert.ok(element('#printers').innerHTML.includes(lang==='en' ? 'New roll' : 'Nieuwe rol'));
assert.ok(element('#jbody').innerHTML.includes(lang==='en' ? 'Older history entry' : 'Oud historiebericht'));
assert.equal(element('#t-avg').textContent,'–');
