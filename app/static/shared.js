export const $ = (selector, scope=document) => scope.querySelector(selector);
export const $$ = (selector, scope=document) => [...scope.querySelectorAll(selector)];
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const uuid = () => crypto.randomUUID();
export const dateLabel = value => new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Minsk',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(value));
export const timeLabel = value => new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Minsk',hour:'2-digit',minute:'2-digit'}).format(new Date(value));
export const localDate = (offset=0) => {const d=new Date(Date.now()+offset*86400000);return new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Minsk',year:'numeric',month:'2-digit',day:'2-digit'}).format(d)};
export async function api(path, {method='GET', data, token, ...options}={}) {
  const headers = data!==undefined ? {'Content-Type':'application/json'} : {};
  if(token)headers.Authorization=`Bearer ${token}`;
  let response;
  try { response=await fetch('/api/v1'+path,{method,headers,body:data!==undefined?JSON.stringify(data):undefined,...options}); }
  catch { throw new Error('Сервер недоступен. Проверьте соединение и повторите.'); }
  const result=response.status===204?null:await response.json();
  if(!response.ok){const e=new Error(result?.error?.message||'Не удалось выполнить запрос');e.status=response.status;e.code=result?.error?.code;throw e;}
  return result;
}
let toastTimer;
export function toast(text){const el=$('#toast');if(!el)return;el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,5000)}
export function bubble(container,role,text){const node=document.createElement('div');node.className='bubble '+role;const label=document.createElement('span');label.className='bubble-label';label.textContent=role==='user'?'ВЫ':role==='operator'?'СОТРУДНИК':'ТОЧКА';node.append(label,document.createTextNode(text));container.append(node);container.scrollTop=container.scrollHeight;return node;}
export function bindDialogs(){for(const button of $$('[data-close]'))button.addEventListener('click',()=>button.closest('dialog').close());for(const dialog of $$('dialog'))dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close()}});}
export class VoiceConnection {
  constructor(audio,onState){this.audio=audio;this.onState=onState;this.pc=null;this.stream=null;this.channel=null;}
  async connect(offerPath,token){
    if(!navigator.mediaDevices?.getUserMedia)throw new Error('Для микрофона нужен localhost или HTTPS. Откройте приложение по защищённой ссылке.');
    try{
      this.stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      this.pc=new RTCPeerConnection({iceServers:[]});
      for(const track of this.stream.getTracks())this.pc.addTrack(track,this.stream);
      this.channel=this.pc.createDataChannel('chat');
      this.pc.ontrack=e=>{this.audio.srcObject=e.streams[0]||new MediaStream([e.track]);this.audio.play().catch(()=>{this.audio.dispatchEvent(new Event('playback-blocked'));this.onState('playback_blocked')});};
      this.pc.onconnectionstatechange=()=>this.onState(this.pc?.connectionState);
      await this.pc.setLocalDescription(await this.pc.createOffer());
      if(this.pc.iceGatheringState!=='complete')await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Не удалось подготовить соединение')),10000);const listener=()=>{if(this.pc?.iceGatheringState==='complete'){clearTimeout(timer);this.pc.removeEventListener('icegatheringstatechange',listener);resolve()}};this.pc.addEventListener('icegatheringstatechange',listener)});
      const answer=await api(offerPath,{method:'POST',data:{sdp:this.pc.localDescription.sdp,type:'offer'},token});
      await this.pc.setRemoteDescription(answer);
      const start=Date.now();
      await new Promise((resolve,reject)=>{const check=setInterval(()=>{if(this.pc?.connectionState==='connected'){clearInterval(check);resolve()}else if(!this.pc||['failed','closed'].includes(this.pc.connectionState)||Date.now()-start>15000){clearInterval(check);reject(new Error('Соединение не установлено. Проверьте сеть и попробуйте снова.'))}},100)});
    }catch(error){await this.close();if(error.name==='NotAllowedError')throw new Error('Нет доступа к микрофону. Разрешите его в настройках браузера или напишите в чат.');if(error.name==='NotFoundError')throw new Error('Микрофон не найден. Подключите его или напишите в чат.');throw error;}
  }
  async resumeAudio(){await this.audio.play();}
  mute(muted){for(const track of this.stream?.getAudioTracks()||[])track.enabled=!muted;}
  async close(){if(this.pc){this.pc.onconnectionstatechange=null;this.pc.close();this.pc=null}for(const track of this.stream?.getTracks()||[])track.stop();this.stream=null;this.audio.srcObject=null;}
}
