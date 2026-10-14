import fs from 'node:fs/promises';
import sharp from 'sharp';
import {Presentation,PresentationFile} from '@oai/artifact-tool';
import {applyPresentationChartFont} from '/Users/andreevich/.codex/plugins/cache/openai-primary-runtime/presentations/26.1009.11318/skills/presentations/container_tools/artifact_tool_utils.mjs';
const root='/Users/andreevich/Documents/Projects/codex/voice',out=root+'/.codex-presentation/build';
const notes=JSON.parse(await fs.readFile(out+'/notes.json','utf8'));
for(const mode of ['dark','light'])await sharp(root+'/app/static/brand/tochka-'+mode+'.svg',{density:192}).resize({width:2400}).png().toFile(out+'/logo-'+mode+'.png');
const C={bg:'#f2f0f4',ink:'#17121d',dark:'#221d29',aub:'#2f2838',ash:'#b9b4c2',dim:'#656170',line:'#d9d5de',white:'#ffffff'};
const p=Presentation.create({slideSize:{width:1280,height:720}});p.theme.defaultFont='Golos Text';
const buf=async path=>{let b=await fs.readFile(path);return b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength)};
async function im(s,path,x,y,w,h,fit='contain'){return s.images.add({blob:await buf(path),contentType:'image/png',fit,position:{left:x,top:y,width:w,height:h}})}
function tx(s,text,x,y,w,h,size=27,color=C.ink,name='body',align='left'){
const q=s.shapes.add({geometry:'textbox',name,position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});q.text=text;q.text.style={typeface:'Golos Text',fontSize:size,color,alignment:align,verticalAlignment:'top',insets:0,autoFit:'none',wrap:'none'};return q;}
function box(s,text,x,y,w,h,{dark=false,name='diagram',fill,sz=25}={}){let q=s.shapes.add({geometry:'roundRect',name,position:{left:x,top:y,width:w,height:h},borderRadius:6,fill:fill??(dark?C.aub:C.white),line:{fill:dark?C.ash:C.line,width:1}});q.text=text;q.text.style={typeface:'Golos Text',fontSize:sz,color:dark?C.bg:C.ink,alignment:'center',verticalAlignment:'middle',insets:14};return q;}
function link(s,a,b,{dark=false,from='right',to='left',g=0}={}){let z=s.shapes.connect(a,b,{kind:'elbow',fromSide:from,toSide:to,line:{fill:dark?C.ash:C.dim,width:1.5},tail:{type:'triangle',width:'sm',length:'sm'}});if(g)z.name='reveal-'+g;return z;}
async function slide(title,{dark=false,sub='',note='',idx=0}={}){let s=p.slides.add();s.background.fill=dark?C.dark:C.bg;await im(s,out+'/logo-'+(dark?'light':'dark')+'.png',64,35,206,36);tx(s,String(p.slides.items.length).padStart(2,'0'),1157,42,57,29,18,dark?C.ash:C.dim,'static-page','right');if(title)tx(s,title,64,119,1152,129,50,dark?C.bg:C.ink,'title');if(sub)tx(s,sub,66,205,1148,73,25,dark?C.ash:C.dim,'subtitle');s.speakerNotes.text=note||notes[idx];return s;}
function item(s,num,title,body,x,y,w=500,dark=false){let c=dark?C.bg:C.ink,d=dark?C.ash:C.dim;tx(s,num,x,y,45,42,23,d,'reveal-'+num);tx(s,title,x+67,y-3,w-67,49,29,c,'reveal-'+num);tx(s,body,x+67,y+47,w-67,88,24,d,'reveal-'+num);}

let s=await slide('',{dark:true,idx:0});s.images.items[0].position={left:64,top:70,width:540,height:94.27};tx(s,'ИИ-агент для\nвходящих звонков',64,273,1152,182,73,C.bg,'hero');tx(s,'Для предпринимателей, которые не всегда\nмогут ответить клиенту лично',68,514,1050,108,31,C.ash,'reveal-1');tx(s,'Концепция сервиса для экосистемы МТС',68,650,1090,35,21,C.ash,'static-disclosure');

s=await slide('Входящие звонки\nмежду встречами',{idx:1});await im(s,out+'/client.png',808,0,472,720,'cover');tx(s,'Владелец небольшой IT-компании',64,304,700,48,30,C.ink,'reveal-1');tx(s,'Весь день в поездках и на совещаниях.\nНе может отвечать на незнакомые номера.',64,366,703,95,26,C.dim,'reveal-1');tx(s,'Что теряет бизнес',64,501,694,49,29,C.ink,'reveal-2');tx(s,'Обращение остаётся без ответа,\nа потенциальный клиент может уйти.',64,560,698,92,27,C.dim,'reveal-2');

s=await slide('Что получает бизнес после звонка',{sub:'Агент принимает звонок, уточняет запрос и сохраняет итог',idx:2});
item(s,'01','Принимает звонок','Начинает диалог, когда владелец занят',64,302,650);
item(s,'02','Уточняет запрос','Выясняет цель обращения и детали',64,438,650);
item(s,'03','Фиксирует результат','Сохраняет итог для дальнейшей работы',64,574,650);
tx(s,'Что получает владелец',819,299,397,60,28,C.ink,'reveal-1');tx(s,'Контакт клиента\n\nСуть обращения\n\nСледующее действие',819,376,397,220,27,C.dim,'reveal-2');tx(s,'Сложный вопрос — человеку',819,626,397,49,23,C.ink,'reveal-3');

s=await slide('Каскад моделей управляет нагрузкой',{dark:true,sub:'Лёгкая модель решает типовые задачи. Тяжёлая подключается к сложным.',idx:3});
let a=box(s,'Речь\nклиента',64,331,166,102,{dark:true,name:'reveal-1'}),b=box(s,'STT\nРечь в текст',273,331,198,102,{dark:true,name:'reveal-1'}),c=box(s,'Лёгкая LLM\nАдрес, график, FAQ',535,294,306,108,{dark:true,name:'reveal-2'}),d=box(s,'Тяжёлая LLM\nДетали, сложный запрос',535,497,306,108,{dark:true,name:'reveal-3',sz:24}),e=box(s,'TTS\nГолосовой ответ',936,331,280,102,{dark:true,name:'reveal-4'});
link(s,a,b,{dark:true,g:1});link(s,b,c,{dark:true,g:2});link(s,c,e,{dark:true,g:4});link(s,c,d,{dark:true,from:'bottom',to:'top',g:3});link(s,d,e,{dark:true,to:'bottom',g:4});tx(s,'Если требуется\nглубокий анализ',706,421,220,60,20,C.ash,'reveal-3');tx(s,'Цель пилота: измерить качество ответа и расход вычислительных ресурсов',64,660,1152,33,22,C.ash,'static-disclosure');

s=await slide('Путь клиента и точки доверия',{sub:'Шесть этапов: от первого интереса до регулярного использования',idx:4});
const journey=[['01','Понять ценность','Показать, какие обращения\nпомогает сохранить агент'],['02','Попробовать','Демо и пробный период\nдля оценки пользы'],['03','Подключить','Понятные настройки\nи условия работы с данными'],['04','Проверить точность','Примеры диалогов\nи ручная коррекция'],['05','Сохранить контроль','История обращений\nи перевод на человека'],['06','Расширить сценарии','CRM / email и аналитика\nпосле проверки в пилоте']];
journey.forEach((v,i)=>{let x=64+(i%3)*399,y=i<3?312:522;tx(s,v[0],x,y,44,39,21,C.dim,'reveal-'+(i+1));tx(s,v[1],x+56,y-3,315,45,28,C.ink,'reveal-'+(i+1));tx(s,v[2],x+56,y+55,315,93,23,C.dim,'reveal-'+(i+1));});

s=await slide('Подключение в привычном приложении',{dark:true,sub:'Предлагаемая интеграция с «Мой МТС»',idx:5});
item(s,'01','Активировать услугу','В приложении оператора,\nна существующем номере',64,309,625,true);item(s,'02','Выбрать сценарий','Указать правила обработки\nи передачи звонков',64,445,625,true);item(s,'03','Сделать пробный звонок','Проверить ответ агента\nи результат обращения',64,581,625,true);
tx(s,'Что согласовать с МТС',804,309,412,60,28,C.bg,'reveal-1');tx(s,'Голосовую платформу\n\nРазмещение и обработку данных\n\nБиллинг и лимиты использования\n\nПоддержку и передачу человеку',804,393,412,265,23,C.ash,'reveal-2');

s=await slide('Подписка и распределение выручки',{sub:'Предварительная коммерческая модель',idx:6});
tx(s,'9,90',64,299,420,138,105,C.ink,'reveal-1');tx(s,'BYN / месяц за абонента',68,441,446,54,28,C.dim,'reveal-1');
let l=box(s,'50% оператору\n4,95 BYN',636,306,280,149,{name:'reveal-2',fill:C.white,sz:29}),r=box(s,'50% команде\n4,95 BYN',936,306,280,149,{name:'reveal-2',fill:C.white,sz:29});
tx(s,'При 1 000 платных абонентов',64,551,500,89,29,C.ink,'reveal-3');tx(s,'9 900 BYN',636,548,280,56,36,C.ink,'reveal-3');tx(s,'общая выручка / мес.',636,607,282,39,23,C.dim,'reveal-3');tx(s,'4 950 BYN',936,548,280,56,36,C.ink,'reveal-3');tx(s,'доля команды / мес.',936,607,280,39,23,C.dim,'reveal-3');
tx(s,'Суммы до расходов и налогов. Лимиты и условия партнёрства требуют согласования.',64,675,1152,30,18,C.dim,'static-disclosure');

s=await slide('Из чего складываются расходы',{sub:'Расчётные ставки из исходной модели',idx:7});
const vals=[['Компонент','Ставка','Единица расчёта'],['Телефония (SIP)','0,05 BYN','1 минута'],['Распознавание и синтез речи','0,05 BYN','1 минута'],['Лёгкая LLM','0,02 BYN','1 000 токенов'],['Тяжёлая LLM','0,08 BYN','1 000 токенов']];let tab=s.tables.add({rows:5,columns:3,left:64,top:291,width:1152,height:280,columnWidths:[570,276,306],values:vals});tab.borders.assign({fill:C.line,width:0.5});
for(let r=0;r<5;r++){tab.rows[r].height=r===0?48:58;for(let c=0;c<3;c++){let ce=tab.getCell(r,c);ce.fill=C.bg;ce.text.style={typeface:'Golos Text',fontSize:r===0?20:25,color:r===0?C.dim:C.ink,insets:12,verticalAlignment:'middle'};}}
tx(s,'0,10 BYN за минуту',64,616,525,53,33,C.ink,'reveal-1');tx(s,'до расходов на модели,\nинфраструктуру и поддержку',665,616,551,69,24,C.dim,'reveal-1');

s=await slide('Минуты меняют экономику подписки',{sub:'Пример: расходы на телефонию и речь оплачиваются из доли команды',note:'Иллюстративный расчёт, а не прогноз спроса. Допущение: базовые расходы 0,10 BYN за минуту оплачиваются из доли команды 4,95 BYN на абонента в месяц. 10 минут: базовые расходы 1,00 BYN, остаток 3,95 BYN. 30 минут: расходы 3,00 BYN, остаток 1,95 BYN. Остаток не является прибылью: из него ещё нужно покрыть LLM, инфраструктуру, разработку, поддержку и налоги. Фактическая модель зависит от распределения расходов между партнёрами. Источник ставок: исходная «Точка.pptx», слайды 6–7. Все суммы BYN на одного абонента за месяц.'});
let chart=s.charts.add('bar',{position:{left:48,top:306,width:824,height:244},categories:['30 минут','10 минут'],series:[{name:'Телефония и речь',values:[3,1],fill:C.aub,dataLabels:{showValue:true,position:'center',numberFormatCode:'0.00',textStyle:{fill:C.bg,fontSize:25,bold:false}}},{name:'Остаток до прочих расходов',values:[1.95,3.95],fill:C.line,dataLabels:{showValue:true,position:'center',numberFormatCode:'0.00',textStyle:{fill:C.ink,fontSize:25,bold:false}}}],barOptions:{direction:'bar',grouping:'stacked',overlap:100,gapWidth:95},hasLegend:false,xAxis:{visible:true,textStyle:{fontSize:24,fill:C.ink,bold:false},line:{fill:'none',width:0},majorGridlines:null},yAxis:{visible:false,min:0,max:4.95,majorGridlines:null},chartFill:C.bg,chartLine:{fill:'none',width:0},plotAreaFill:C.bg,plotAreaLine:{fill:'none',width:0}});applyPresentationChartFont(chart,{fontFamily:'Golos Text'});
tx(s,'4,95 BYN',920,312,295,65,44,C.ink,'reveal-1');tx(s,'доля команды\nс одной подписки',920,390,295,90,27,C.dim,'reveal-1');
tx(s,'Тёмная часть',64,563,370,34,21,C.ink,'static-legend');tx(s,'Телефония + распознавание + синтез',64,605,600,38,22,C.dim,'static-legend');tx(s,'Светлая часть',716,563,500,34,21,C.ink,'static-legend');tx(s,'Остаток до LLM и других расходов',716,605,500,38,22,C.dim,'static-legend');tx(s,'BYN / абонент / месяц. Лимиты минут нужно определить до запуска тарифа.',64,675,1152,30,18,C.dim,'static-disclosure');

s=await slide('Демонстрация одного обращения',{sub:'Сценарий: вопрос о компании и просьба связаться с сотрудником',idx:8});
tx(s,'«Вы сегодня работаете?\nХочу обсудить\nподключение»',64,318,696,197,44,C.ink,'reveal-1');tx(s,'Пример запроса для живой демонстрации',67,557,670,44,22,C.dim,'static-disclosure');
item(s,'01','Ответ по знаниям','Агент использует\nданные компании',820,310,396);item(s,'02','Уточнение деталей','Выясняет цель обращения\nи контакт клиента',820,440,396);item(s,'03','Результат в кабинете','Владелец видит итог\nи следующий шаг',820,570,396);

s=await slide('Пилот перед масштабированием',{dark:true,sub:'Проверить пользу для клиента и стоимость реальной нагрузки',idx:9});
tx(s,'На пилоте',64,317,610,57,33,C.bg,'reveal-1');tx(s,'Качество',64,408,202,43,27,C.bg,'reveal-2');tx(s,'Доля завершённых обращений',295,408,470,49,25,C.ash,'reveal-2');tx(s,'Скорость',64,482,202,43,27,C.bg,'reveal-3');tx(s,'Задержка голосового ответа',295,482,470,49,25,C.ash,'reveal-3');tx(s,'Экономика',64,556,202,43,27,C.bg,'reveal-4');tx(s,'Стоимость минуты и подписки',295,556,470,49,25,C.ash,'reveal-4');
tx(s,'Следующий этап',854,317,362,57,33,C.bg,'reveal-1');tx(s,'Интеграции CRM / email\n\nАналитика частых вопросов\n\nУлучшение сценариев\nпо реальным диалогам',854,408,362,219,24,C.ash,'reveal-5');
tx(s,'Первый шаг: согласовать сценарии, группу абонентов и критерии успеха',64,667,1152,36,22,C.bg,'reveal-6');

await fs.mkdir(out+'/revised',{recursive:true});for(let i=0;i<p.slides.items.length;i++){const png=await p.export({slide:p.slides.items[i],format:'png',scale:1.25});await fs.writeFile(out+`/revised/${i+1}.png`,new Uint8Array(await png.arrayBuffer()));}
await(await PresentationFile.exportPptx(p)).save(out+'/draft.pptx');console.log('Revised deck:',p.slides.items.length,'slides');
