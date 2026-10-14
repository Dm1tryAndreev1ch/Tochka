import {readFileSync,writeFileSync} from 'node:fs';
const dir = new URL('./',import.meta.url);
let svg = readFileSync(new URL('tochka-waveform-inverse.svg',dir),'utf8');
svg=svg.replace('viewBox="180 115 1708 436"','viewBox="60 100 1948 468"').replace('M180 115h1708v436H180z','M60 100h1948v468H60z');
const tail = (xs,hs) => xs.map((x,i)=>`<rect x="${x}" y="${338-hs[i]/2}" width="16" height="${hs[i]}" rx="8"/>`).join('\n');
svg = svg.replace(/<g id="wave-in">[\s\S]*?<\/g>/,`<g id="wave-in">${tail([134,162,190,218,246,274,302,330,358],[16,28,18,44,30,72,50,110,78])}</g>`);
svg = svg.replace(/<g id="wave-out">[\s\S]*?<\/g>/,`<g id="wave-out">${tail([1684,1712,1740,1768,1796,1824,1852,1880,1908,1936],[100,136,76,104,52,70,34,42,24,16])}</g>`);
svg = svg.replace(/<g id="letter-[\s\S]*?<\/g>/g, block=>block.replace(/<rect x="([\d.]+)" y="([\d.]+)" width="16" height="([\d.]+)" rx="8"\/>/g,(match,xx,yy,hh)=>{
 const x=+xx;let y=+yy,h=+hh;
 const index=Math.round(x/26);
 const variance=[0,14,-6,30,4,-10,22,-4,10,36,-8][index%11];
 if(y<275){y-=variance;h+=variance;}
 if(+yy + +hh > 420) h += [8,-4,22,0,-10,14,32,-6,10][index%9];
 return `<rect x="${x}" y="${y}" width="16" height="${h}" rx="8"/>`;
}));
svg=svg.replace('</g>\n</svg>', '<g id="signal-between-letters"><rect x="1140" y="328" width="16" height="20" rx="8"/><rect x="1386" y="323" width="16" height="30" rx="8"/></g>\n</g>\n</svg>');
for (const [name,fg,bg] of [['inverse','#f2f0f4','#2f2838'],['light','#2f2838','#f2f0f4'],['transparent','#2f2838',null],['white-transparent','#f2f0f4',null]]) {
 let variant=svg.replace('<g fill="#f2f0f4">',`<g fill="${fg}">`);
 variant=variant.replace('<path fill="#2f2838" d="M60 100h1948v468H60z"/>',bg?`<path fill="${bg}" d="M60 100h1948v468H60z"/>`:'');
 writeFileSync(new URL(`tochka-waveform-v2-${name}.svg`,dir),variant);
}
console.log('Four waveform v2 SVG variants written.');
