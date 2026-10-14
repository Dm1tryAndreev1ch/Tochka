import {readFileSync,writeFileSync} from 'node:fs';
const reference=JSON.parse(readFileSync(new URL('./approved-reference-geometry.json',import.meta.url),'utf8'));
const sections=[['waveform-intro',0,6],['letter-t',6,13],['transition-t-o',13,14],['letter-o',14,21],['transition-o-che',21,22],['letter-che',22,29],['transition-che-ka',29,31],['letter-ka',31,37],['transition-ka-a',37,38],['letter-a',38,47],['waveform-outro',47,55]];
const groups=sections.map(([id,start,end])=>[id,reference.columns.slice(start,end).flatMap(([x,w,spans])=>spans.map(([y,h])=>[x+(w-11)/2,y,11,h]))]);
const rects=groups.flatMap(([,rs])=>rs);
for(let i=0;i<rects.length;i++)for(let j=i+1;j<rects.length;j++){
 const [x,y,w,h]=rects[i],[a,b,c,d]=rects[j];
 if(x<a+c&&x+w>a&&y<b+d&&y+h>b)throw new Error('Overlapping bars');
}
const {width,height}=reference;
for(const[name,fg,bg]of [['tochka-waveform','#d9d9d9','#1e1e1e'],['tochka-waveform-transparent','#d9d9d9',null],['tochka-waveform-dark-transparent','#1e1e1e',null]]){
 const svg=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="tochka-title tochka-description">\n  <title id="tochka-title">Точка</title>\n  <desc id="tochka-description">Логотип ТОЧКА из округлых полос звуковой волны. Векторная версия по утверждённому образцу: плавное вступление, короткий переход Ч–К и затухающий выход.</desc>\n${bg?`  <path fill="${bg}" d="M0 0h${width}v${height}H0z"/>\n`:''}  <g fill="${fg}">\n${groups.map(([id,rs])=>`    <g id="${id}">\n${rs.map(([x,y,w,h])=>`      <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="5.5"/>`).join('\n')}\n    </g>`).join('\n')}\n  </g>\n</svg>\n`;
 writeFileSync(new URL(`./${name}.svg`,import.meta.url),svg);
}
console.log(`PASS: ${rects.length} vector bars; no intersections; ${groups.filter(([id])=>id.startsWith('letter-')).length} letters; all three SVG variants saved.`);
