import {readFileSync,writeFileSync} from 'node:fs';
const dir=new URL('./',import.meta.url);
let svg=readFileSync(new URL('tochka-waveform-inverse.svg',dir),'utf8');
svg=svg.replace('viewBox="180 115 1708 436"','viewBox="60 100 1948 468"').replace('M180 115h1708v436H180z','M60 100h1948v468H60z');
const tail=(xs,hs)=>xs.map((x,i)=>`<rect x="${x}" y="${338-hs[i]/2}" width="16" height="${hs[i]}" rx="8"/>`).join('\n');
svg=svg.replace(/<g id="wave-in">[\s\S]*?<\/g>/,`<g id="wave-in">${tail([134,162,190,218,246,274,302,330,358],[16,24,38,56,76,92,100,96,78])}</g>`);
svg=svg.replace(/<g id="wave-out">[\s\S]*?<\/g>/,`<g id="wave-out">${tail([1684,1712,1740,1768,1796,1824,1852,1880,1908,1936],[78,96,100,92,76,56,38,24,18,16])}</g>`);
svg=svg.replace('x="498" y="197" width="16" height="264"','x="498" y="202" width="16" height="254"');
svg=svg.replace('x="758" y="197" width="16" height="60"','x="758" y="204" width="16" height="60"');
svg=svg.replace('x="758" y="406" width="16" height="62"','x="758" y="402" width="16" height="61"');
svg=svg.replace('x="1204" y="199" width="16" height="262"','x="1204" y="204" width="16" height="252"');
svg=svg.replace('x="1542" y="197" width="16" height="96"','x="1542" y="205" width="16" height="94"');
svg=svg.replace('</g>\n</svg>',`<g id="signal-connections">${tail([394,420,446,550,576,602,1140,1386],[34,22,16,16,24,38,20,28])}</g>\n</g>\n</svg>`);
for(const [name,fg,bg] of [['inverse','#f2f0f4','#2f2838'],['light','#2f2838','#f2f0f4'],['transparent','#2f2838',null],['white-transparent','#f2f0f4',null]]){
 let variant=svg.replace('<g fill="#f2f0f4">',`<g fill="${fg}">`).replace('<path fill="#2f2838" d="M60 100h1948v468H60z"/>',bg?`<path fill="${bg}" d="M60 100h1948v468H60z"/>`:'');
 writeFileSync(new URL(`tochka-waveform-smooth-${name}.svg`,dir),variant);
}
console.log('Created four smooth SVG variants.');
