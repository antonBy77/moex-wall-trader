"""bookmap_html — Bookmap-подобная визуализация глубины стакана в браузере.

Читает NDJSON коллектора, строит тепловую карту (цена × время): яркость = размер
лимитных заявок, зелёный/красный оттенок = bid/ask сторона, точки = сделки
(размер = объём), жёлтые линии = самые толстые стены дня. Zoom/панорама мышью,
тултип с ценой/объёмом/временем.

Запуск:
    python3 bookmap_html.py --replay data/SBER-MISX-2026-09-24.ndjson
    -> bookmap.html, открыть в браузере (двойной клик)
"""
import argparse
import json
import math
import os
from datetime import datetime

GROUP = 0.05          # группировка цены, ₽ (одна строка = 5 копеек)
COL_SEC = 20          # одна колонка = 20 секунд


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True)
    ap.add_argument("--out", default="bookmap.html")
    ap.add_argument("--group", type=float, default=GROUP)
    ap.add_argument("--col-sec", type=int, default=COL_SEC)
    a = ap.parse_args()

    rows = {}          # price_key -> row index
    row_prices = []
    cols = {}          # time_key -> col index
    col_times = []
    bids = {}          # (row, col) -> size
    asks = {}
    trades = []        # [col, row, size, dir]
    t0 = None

    def row_of(p):
        k = round(round(p / a.group) * a.group, 4)
        if k not in rows:
            rows[k] = len(row_prices)
            row_prices.append(k)
        return rows[k]

    def col_of(dt):
        nonlocal t0
        if t0 is None:
            t0 = dt
        k = int((dt - t0).total_seconds() // a.col_sec)
        if k not in cols:
            cols[k] = len(col_times)
            col_times.append(k * a.col_sec)
        return cols[k]

    with open(a.replay) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "q" in r:
                if r["q"] != "ob" or not r.get("bid") or not r.get("ask"):
                    continue
                try:
                    dt = datetime.fromisoformat(r["t"])
                except Exception:
                    continue
                c = col_of(dt)
                for p, s in r["bid"]:
                    key = (row_of(p), c)
                    bids[key] = bids.get(key, 0) + float(s)
                for p, s in r["ask"]:
                    key = (row_of(p), c)
                    asks[key] = asks.get(key, 0) + float(s)
            else:
                try:
                    dt = datetime.fromisoformat(r["t"])
                except Exception:
                    continue
                trades.append([col_of(dt), row_of(float(r["p"])),
                               float(r["s"]), 1 if r["d"] > 0 else -1])

    # средние за колонку (стакан шёл 2с, колонка 20с -> до 10 снапшотов на ячейку)
    n_ob = {}
    for d in (bids, asks):
        for k in d:
            n_ob[k] = n_ob.get(k, 0) + 1
    for d in (bids, asks):
        for k in list(d):
            d[k] = d[k] / max(1, n_ob.get(k, 1))

    # --- CVD и минутные volume bars ---
    cvd_series = []       # [col, cvd]
    vol_bars = []         # [col, buy, sell]
    minutes = {}          # minute_index -> [buy, sell]
    cvd = 0.0
    trades.sort(key=lambda t: t[0])
    for c, r, s, d in trades:
        cvd += s * d
        cvd_series.append([c, round(cvd)])
        mi = int(col_times[c] // 60)
        m = minutes.setdefault(mi, [0.0, 0.0])
        m[0 if d > 0 else 1] += s
    for mi, (b, sl) in sorted(minutes.items()):
        vol_bars.append([mi, round(b), round(sl)])

    data = {
        "group": a.group,
        "colSec": a.col_sec,
        "nRows": len(row_prices),
        "nCols": len(col_times),
        "rowPrices": row_prices,
        "bids": [[r, c, round(s)] for (r, c), s in bids.items()],
        "asks": [[r, c, round(s)] for (r, c), s in asks.items()],
        "trades": trades,
        "cvd": cvd_series,
        "volBars": vol_bars,
    }
    js = json.dumps(data, separators=(",", ":"))
    size_mb = len(js) / 1e6
    print(f"ячеек bid/ask: {len(bids)}/{len(asks)}, сделок: {len(trades)}, "
          f"строк: {len(row_prices)}, колонок: {len(col_times)}, JSON {size_mb:.1f}МБ")

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Bookmap MOEX — SBER</title>
<style>
  html,body{margin:0;background:#0b0e14;color:#c9d1d9;font-family:Menlo,Consolas,monospace;overflow:hidden}
  #cv{display:block;cursor:crosshair}
  #tip{position:fixed;background:#161b22;border:1px solid #30363d;padding:6px 9px;
       font-size:12px;border-radius:6px;pointer-events:none;display:none;z-index:5;line-height:1.5}
  #bar{position:fixed;top:8px;left:12px;font-size:12px;color:#8b949e;background:#0b0e14cc;
       padding:4px 10px;border-radius:6px;z-index:4}
</style></head><body>
<canvas id="cv"></canvas><div id="tip"></div>
<div id="bar">колёсико=зум · перетаскивание=скролл · двойной клик=вся картина</div>
<script>
const D = __DATA__;
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const tip = document.getElementById('tip');
let W,H, view={x:0,y:0,cellW:6,cellH:8};
function resize(){W=cv.width=innerWidth;H=cv.height=innerHeight;draw();}
addEventListener('resize',resize);

function t2c(sec){ // кол-во секунд от старта -> x
  return sec / D.colSec;
}
function heat(v, mx, side){
  const f = Math.min(1, Math.log10(1+v)/Math.log10(1+mx));
  // bookmap-подобная шкала: тёмный -> насыщенный -> белый
  if (f < .25) return side>0 ? `rgb(0,${40+f*160|0},80)` : `rgb(${40+f*160|0},20,40)`;
  if (f < .5)  return side>0 ? `rgb(0,${120+f*200|0},120)` : `rgb(${150+f*150|0},50,60)`;
  if (f < .8)  return side>0 ? `rgb(${60+f*150|0},${210|0},${100+f*80|0})` : `rgb(${230|0},${90+f*80|0},${100+f*80|0})`;
  const w = (f-.8)/.2;
  return side>0 ? `rgb(${140+w*100|0},255,${160+w*90|0})` : `rgb(255,${160+w*80|0},${160+w*80|0})`;
}
let mxB=0,mxA=0;
for(const [, ,s] of D.bids) mxB=Math.max(mxB,s);
for(const [, ,s] of D.asks) mxA=Math.max(mxA,s);
const mx=Math.max(mxB,mxA);

function draw(){
  const cellWv=cellW(), cellHv=view.cellH;
  const chartH=H-58;                       // снизу: volume bars 44 + время 14
  ctx.fillStyle='#0b0e14'; ctx.fillRect(0,0,W,H);
  const x0=Math.max(0,Math.floor(view.x/cellWv)), x1=Math.min(D.nCols,Math.ceil((view.x+W)/cellWv));
  const y0=Math.max(0,Math.floor(view.y/cellHv)), y1=Math.min(D.nRows,Math.ceil((view.y+chartH)/cellHv));
  // ячейки глубины
  for(const [r,c,s] of D.bids){
    const x=c*cellWv-view.x, y=r*cellHv-view.y;
    if(x<-cellWv||x>W||y<-cellHv||y>chartH) continue;
    ctx.fillStyle=heat(s,mx,1);
    ctx.fillRect(x,y,Math.max(1,cellWv-0.3),Math.max(1,cellHv-0.3));
  }
  for(const [r,c,s] of D.asks){
    const x=c*cellWv-view.x, y=r*cellHv-view.y;
    if(x<-cellWv||x>W||y<-cellHv||y>chartH) continue;
    ctx.fillStyle=heat(s,mx,-1);
    ctx.fillRect(x,y,Math.max(1,cellWv-0.3),Math.max(1,cellHv-0.3));
  }
  // сделки
  for(const [c,r,s,d] of D.trades){
    const x=(c+0.5)*cellWv-view.x, y=(r+0.5)*cellHv-view.y;
    if(x<-10||x>W+10||y<-10||y>chartH+10) continue;
    const rad=Math.min(9, 2+Math.log2(1+s)/2);
    ctx.fillStyle=d>0?'rgba(60,220,130,.95)':'rgba(255,90,100,.95)';
    ctx.beginPath();ctx.arc(x,y,rad,0,7);ctx.fill();
  }
  // сетка цен
  ctx.fillStyle='#8b949e'; ctx.font='11px Menlo';
  const stepR=Math.max(1, Math.round(24/cellHv));
  for(let r=y0;r<y1;r++){
    if(r%stepR) continue;
    const y=r*cellHv-view.y+cellHv/2+4;
    ctx.fillText(D.rowPrices[r].toFixed(2), 8, y);
    ctx.fillStyle='#21262d'; ctx.fillRect(0,y-4,W,0.5); ctx.fillStyle='#8b949e';
  }
  // сетка времени
  const stepC=Math.max(1, Math.round(90/cellWv));
  for(let c=x0;c<x1;c++){
    if(c%stepC) continue;
    const x=c*cellWv-view.x;
    const sec=c*D.colSec, hh=Math.floor(sec/3600)+10, mm=Math.floor(sec%3600/60);
    ctx.fillText(`${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}`, x+3, H-6);
    ctx.fillStyle='#21262d'; ctx.fillRect(x,0,0.5,chartH); ctx.fillStyle='#8b949e';
  }
  // === VOLUME BARS (минутные buy/sell, полоса снизу) ===
  const vbY=chartH+4, vbH=40;
  let mxBar=0; for(const [,b,s] of D.volBars) mxBar=Math.max(mxBar,b+s);
  const bw=Math.max(2, cellWv*(60/D.colSec)*0.7);
  ctx.fillStyle='#8b949e'; ctx.fillText('vol/min', 8, vbY+10);
  for(const [mi,b,s] of D.volBars){
    const x=(mi*60/D.colSec)*cellWv-view.x;
    if(x<-20||x>W+20) continue;
    const hs=(s/mxBar)*vbH, hb=(b/mxBar)*vbH;
    ctx.fillStyle='rgba(255,90,100,.75)'; ctx.fillRect(x,vbY+vbH-hs,bw,hs);
    ctx.fillStyle='rgba(60,220,130,.75)'; ctx.fillRect(x,vbY+vbH-hs-hb,bw,hb);
  }
  // === CVD (полоса сверху) ===
  if(D.cvd&&D.cvd.length>1){
    const cY=4, cH=22;
    let mn=Infinity,mx2=-Infinity;
    for(const [,v] of D.cvd){if(v<mn)mn=v;if(v>mx2)mx2=v;}
    const rng=(mx2-mn)||1;
    ctx.strokeStyle='#d29922'; ctx.lineWidth=1.4; ctx.beginPath();
    let st=false;
    for(const [c,v] of D.cvd){
      const x=(c+0.5)*cellWv-view.x;
      if(x<-5||x>W+5){st=false;continue;}
      const y=cY+cH-((v-mn)/rng)*cH;
      if(!st){ctx.moveTo(x,y);st=true;}else ctx.lineTo(x,y);
    }
    ctx.stroke(); ctx.lineWidth=1;
    ctx.fillStyle='#d29922'; ctx.fillText(`CVD ${mn.toFixed(0)} … ${mx2.toFixed(0)}`, 60, cY+10);
  }
}
function cellW(){return view.cellW;}

// зум колесом вокруг курсора
addEventListener('wheel',e=>{
  e.preventDefault();
  const f=e.deltaY<0?1.25:0.8;
  const nx=Math.max(2,Math.min(60,view.cellW*f)), ny=Math.max(3,Math.min(40,view.cellH*(nx/view.cellW)));
  const mx_=e.clientX, my_=e.clientY;
  view.x=mx_-(mx_+view.x)*(nx/view.cellW);
  view.y=my_-(my_+view.y)*(ny/view.cellH);
  view.cellW=nx; view.cellH=ny;
  clamp(); draw();
},{passive:false});
function clamp(){
  const maxX=D.nCols*view.cellW-W, maxY=D.nRows*view.cellH-H;
  view.x=Math.max(-40,Math.min(maxX+40,view.x));
  view.y=Math.max(0,Math.min(Math.max(0,maxY),view.y));
}
// перетаскивание
let drag=null;
addEventListener('mousedown',e=>drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y});
addEventListener('mousemove',e=>{
  if(drag){view.x=drag.vx-(e.clientX-drag.x);view.y=drag.vy-(e.clientY-drag.y);clamp();draw();}
  // тултип
  const c=Math.floor((e.clientX+view.x)/cellW()), r=Math.floor((e.clientY+view.y)/view.cellH);
  if(c<0||c>=D.nCols||r<0||r>=D.nRows){tip.style.display='none';return;}
  const b=D.bids.find(x=>x[0]===r&&x[1]===c), a2=D.asks.find(x=>x[0]===r&&x[1]===c);
  const sec=c*D.colSec, hh=Math.floor(sec/3600)+10, mm=Math.floor(sec%3600/60), ss=sec%60;
  tip.innerHTML=`${D.rowPrices[r].toFixed(2)} · ${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
    +(b?`<br>bid: <b style="color:#3fb950">${b[2].toLocaleString('ru')}</b>`:'')
    +(a2?`<br>ask: <b style="color:#f85149">${a2[2].toLocaleString('ru')}</b>`:'');
  tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+10)+'px'; tip.style.display='block';
});
addEventListener('mouseup',()=>drag=null);
addEventListener('dblclick',()=>{view={x:D.nCols*cellW()-W,y:(D.nRows*view.cellH-H)/2,cellW:6,cellH:8};clamp();draw();});
resize();
</script></body></html>"""
    html = html.replace("__DATA__", js)
    with open(a.out, "w") as f:
        f.write(html)
    print("saved", os.path.abspath(a.out))


if __name__ == "__main__":
    main()
