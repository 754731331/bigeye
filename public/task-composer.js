/* 输入框里的「任务模型」选择器。
 *
 * 2026-10-08 改版：
 *   1) 加可见把手（下滑/轻点关闭）—— 之前只有一条几乎看不见的细线；
 *   2) 加搜索框（按 名称 / id / 来源 / 描述 实时过滤，Esc 清空、再 Esc 关闭）；
 *   3) 按来源分组（组名 + 数量，当前模型所在源置顶），不再一长串混排；
 *   4) 打开不再等网络：有缓存先出面板，再后台刷新。
 *      服务端 /api/task-model 首次会很慢（本机 llama.cpp 探活要等满 2s 超时），
 *      所以「渲染」与「取数」必须解耦 —— 否则每次点开都是白等 2 秒。
 */
(function(){
  function tid(){return (typeof currentTopic!=='undefined'&&currentTopic)?currentTopic.id:'';}
  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  var ICON_SEARCH='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/></svg>';

  var style=document.createElement('style');
  style.textContent=`
/* ===== 发送行栅格：模型按钮占中间那一格（保持原样，别改） ===== */
footer .input-row{display:grid;grid-template-columns:34px minmax(0,1fr) 36px;gap:3px;padding:5px 8px;border:1px solid var(--border2);border-radius:18px;background:transparent}
footer .input-row textarea{grid-column:1/-1;grid-row:1;min-height:26px;max-height:120px;border:0!important;background:transparent!important;box-shadow:none!important;padding:2px 5px!important;width:100%;resize:none}
footer .input-row .attach-btn{grid-column:1;grid-row:2;height:30px;width:30px}
footer .input-row .send{grid-column:3;grid-row:2;height:30px;width:30px}
.task-model-btn{grid-column:2;grid-row:2;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border:0;background:transparent;color:var(--tdim);font:inherit;font-size:12px;padding:4px 8px;cursor:pointer}

/* ===== 面板：竖向三段（把手/标题 · 搜索 · 列表滚动区） =====
   注意 .task-model-sheet>div 那条外部规则带 overflow-y:auto（特异度更高），
   不 !important 的话面板会自己滚、内部滚动区失效。 */
.tm-panel{display:flex!important;flex-direction:column!important;overflow:hidden!important;
  width:min(100%,560px);background:var(--surface2);box-shadow:0 -10px 40px rgba(0,0,0,.35);
  padding-bottom:env(safe-area-inset-bottom)}
/* 把手：可见的横杠 + 明确的抓取光标 */
.tm-panel>.task-sheet-grip{flex:0 0 auto;display:flex;align-items:center;justify-content:center;
  width:100%;height:24px;padding:0;border:0;background:transparent;cursor:grab;touch-action:none}
.tm-panel>.task-sheet-grip::after{content:'';width:46px;height:5px;border-radius:3px;background:var(--tdim);opacity:.65}
.tm-panel>.task-sheet-grip:hover::after{opacity:.95}
.tm-panel>.task-sheet-grip:active{cursor:grabbing}
.tm-panel>.task-sheet-grip:focus-visible{outline:2px solid var(--accent);outline-offset:-4px;border-radius:8px}
/* 标题行 */
.tm-head{flex:0 0 auto;display:flex;align-items:baseline;gap:8px;min-width:0;padding:2px 18px 8px}
.tm-title{font-size:13px;font-weight:600;color:var(--text);white-space:nowrap}
.tm-sub{flex:1 1 auto;min-width:0;text-align:right;font-size:11px;color:var(--tdim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 搜索框 */
.tm-search{flex:0 0 auto;position:relative;padding:0 14px 8px}
.tm-search>svg{position:absolute;left:25px;top:10px;width:14px;height:14px;color:var(--tdim);pointer-events:none}
.tm-input{width:100%;height:34px;box-sizing:border-box;padding:0 32px;border-radius:10px;
  border:1px solid var(--border2);background:var(--surface3);color:var(--text);
  font:inherit;font-size:13px;outline:none;transition:border-color .15s}
.tm-input::placeholder{color:var(--tdim)}
.tm-input:focus{border-color:var(--accent)}
.tm-clr{position:absolute;right:22px;top:7px;width:20px;height:20px;padding:0;border:0;border-radius:50%;
  background:transparent;color:var(--tdim);font:inherit;font-size:13px;line-height:1;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
.tm-clr:hover{color:var(--text);background:var(--hover)}
/* 列表滚动区 —— 只有这里滚 */
.tm-scroll{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
  padding:0 10px 12px;scrollbar-width:thin}
.tm-scroll::-webkit-scrollbar{width:6px}
.tm-scroll::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.tm-scroll::-webkit-scrollbar-track{background:transparent}
/* 分组 */
.tm-group{margin-top:2px}
.tm-gh{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:6px;
  padding:7px 8px 5px;background:var(--surface2);font-size:11px;color:var(--tdim)}
.tm-gl{font-weight:600;letter-spacing:.02em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-gc{margin-left:auto;font-size:10px;opacity:.75}
/* 条目 */
.tm-item{display:flex;align-items:center;gap:8px;width:100%;box-sizing:border-box;border:0;
  border-radius:9px;background:transparent;color:var(--text);text-align:left;
  font:inherit;font-size:13px;padding:9px 10px;cursor:pointer}
.tm-item:hover{background:var(--hover)}
.tm-item:disabled{opacity:.4;cursor:not-allowed}
.tm-item.on{background:rgba(var(--accent-rgb),.13);box-shadow:inset 0 0 0 1px rgba(var(--accent-rgb),.5)}
.tm-item.on .tm-nm{font-weight:600}
.tm-nm{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-tag{flex:0 0 auto;font-size:10px;padding:1px 6px;border-radius:6px;
  border:1px solid rgba(var(--accent-rgb),.5);color:var(--accent)}
.tm-item.tm-busy{opacity:.5;pointer-events:none}
.tm-empty{padding:28px 16px;text-align:center;color:var(--tdim);font-size:12px;line-height:2}
.tm-retry{border:1px solid var(--border2);background:var(--surface3);color:var(--text);
  border-radius:8px;padding:5px 14px;font:inherit;font-size:12px;cursor:pointer}
.tm-retry:hover{border-color:var(--accent);color:var(--accent)}`;
  document.head.appendChild(style);

  function closeSheet(){
    live=null;
    if(window.__closeModelSheets) window.__closeModelSheets();
    else Array.prototype.forEach.call(document.querySelectorAll('.task-model-sheet'),function(s){s.remove();});
  }

  var row=document.querySelector('footer .input-row'); if(!row) return;
  var button=document.createElement('button');
  button.className='task-model-btn'; button.type='button'; button.textContent='选择模型 ▾';
  row.appendChild(button);

  /* ── 数据层 ─────────────────────────────────────────────
     按任务 id 缓存最近几条：切回刚看过的任务时面板同样秒开。 */
  var CACHE_TTL=30000;      // 30s 内不重复请求（用户连点几次时几乎零延迟）
  var MAX_CACHE=8;
  var cacheMap={}, shown='', pending=null, openSeq=0, lastFail=0, live=null;

  function cacheOf(id){ var e=cacheMap[id]; return e?e.d:null; }
  function cacheFresh(id){ var e=cacheMap[id]; return !!e && (Date.now()-e.at)<CACHE_TTL; }
  function putCache(id,d){
    cacheMap[id]={d:d,at:Date.now()};
    var ks=Object.keys(cacheMap);
    for(var i=0;i<ks.length-MAX_CACHE;i++) delete cacheMap[ks[i]];
  }
  function applyLabel(d){
    if(!d) return;
    button.textContent=(d.model||'选择模型')+' ▾';
    button.title='仅此任务：'+(d.provider||'');
  }

  function load(force){
    var id=tid(); if(!id) return Promise.resolve(null);
    if(pending && pending.__id===id) return pending;          // 在飞的请求复用，连点不重复发
    if(!force && cacheFresh(id)) return Promise.resolve(cacheOf(id));
    var p=(async function(){
      try{
        var r=await fetch('/api/task-model?topic_id='+encodeURIComponent(id));
        var d=await r.json();
        if(tid()!==id) return null;                            // 已切任务，丢弃
        if(!r.ok || !d || d.error) throw Error((d&&d.error)||('HTTP '+r.status));
        putCache(id,d); shown=id; applyLabel(d);
        return d;
      }catch(e){
        return null;
      }finally{
        if(pending===p) pending=null;
      }
    })();
    p.__id=id; pending=p;
    return p;
  }

  /* ── 渲染层 ───────────────────────────────────────────── */
  function groupLabel(p,m){
    if(m && m.group) return m.group;                          // 第三方源：服务端给的显示名
    if(typeof PROVIDER_NAMES!=='undefined' && PROVIDER_NAMES[p]) return PROVIDER_NAMES[p];
    try{ if(typeof cpSourceLabel==='function'){ var l=cpSourceLabel(p); if(l) return l; } }catch(e){}
    return p||'其他';
  }

  function buildList(d,q){
    var models=(d&&d.models)||[];
    if(!models.length) return '<div class="tm-empty">暂无模型</div>';
    var ql=String(q||'').trim().toLowerCase();
    var order=[], byKey={};
    for(var i=0;i<models.length;i++){
      var m=models[i], key=m.provider||'unknown';
      if(!byKey[key]){ byKey[key]={key:key,label:groupLabel(key,m),items:[]}; order.push(key); }
      byKey[key].items.push(m);
    }
    // 当前模型所在的源置顶：换完任务最常见的动作就是确认/微调当前模型
    var curKey=null;
    for(var c=0;c<models.length;c++){ if(models[c].current){ curKey=models[c].provider||'unknown'; break; } }
    if(curKey){ var ci=order.indexOf(curKey); if(ci>0){ order.splice(ci,1); order.unshift(curKey); } }

    var html='', total=0;
    for(var g=0;g<order.length;g++){
      var grp=byKey[order[g]], hit=[];
      for(var k=0;k<grp.items.length;k++){
        var it=grp.items[k];
        if(!ql || (String(it.name||'')+' '+String(it.id||'')+' '+grp.label+' '+String(it.desc||'')).toLowerCase().indexOf(ql)>=0) hit.push(it);
      }
      if(!hit.length) continue;
      total+=hit.length;
      html+='<div class="tm-group"><div class="tm-gh"><span class="tm-gl">'+esc(grp.label)+'</span><span class="tm-gc">'+hit.length+'</span></div>';
      for(var h=0;h<hit.length;h++){
        var mm=hit[h];
        html+='<button type="button" class="tm-item'+(mm.current?' on':'')+'"'+(mm.available===false?' disabled':'')
             +' data-p="'+esc(mm.provider)+'" data-m="'+esc(mm.id)+'">'
             +'<span class="tm-nm">'+esc(mm.name||mm.id)+'</span>'
             +(mm.current?'<span class="tm-tag">当前</span>':'')
             +'</button>';
      }
      html+='</div>';
    }
    if(!total) return '<div class="tm-empty">没有匹配「'+esc(q)+'」的模型</div>';
    return html;
  }

  function makeSheet(id,data,seq){
    closeSheet();
    var cover=document.createElement('div'); cover.className='task-model-sheet';
    var panel=document.createElement('div'); panel.className='tm-panel';
    panel.innerHTML=
      '<button type="button" class="task-sheet-grip" aria-label="下滑或点击关闭模型选择"></button>'
      +'<div class="tm-head"><div class="tm-title">当前任务的模型</div><div class="tm-sub">加载中…</div></div>'
      +'<div class="tm-search">'+ICON_SEARCH
      +'<input class="tm-input" type="text" placeholder="搜索模型或来源…" autocomplete="off" spellcheck="false" aria-label="搜索模型">'
      +'<button type="button" class="tm-clr" hidden aria-label="清空搜索">✕</button></div>'
      +'<div class="tm-scroll"></div>';
    cover.appendChild(panel);
    cover.addEventListener('click',function(ev){ if(ev.target===cover) closeSheet(); });
    document.body.appendChild(cover);

    var scroll=panel.querySelector('.tm-scroll'),
        input=panel.querySelector('.tm-input'),
        clr=panel.querySelector('.tm-clr'),
        sub=panel.querySelector('.tm-sub');

    var self={data:null,seq:seq,id:id,cover:cover,panel:panel,scroll:scroll,sub:sub};
    live=self;

    function repaint(q){
      var query=(q==null)?input.value:q;
      if(self.data){
        scroll.innerHTML=buildList(self.data,query);
        sub.textContent=(self.data.model||'')+(self.data.provider?(' · '+groupLabel(self.data.provider,null)):'');
      }else{
        scroll.innerHTML='<div class="tm-empty">加载中…</div>';
        sub.textContent='加载中…';
      }
    }
    function focusCurrent(){
      if(input.value || !self.data) return;
      var cur=scroll.querySelector('.tm-item.on'); if(!cur) return;
      var a=cur.getBoundingClientRect(), b=scroll.getBoundingClientRect();
      if(a.top<b.top+8 || a.bottom>b.bottom-8) scroll.scrollTop+=(a.top-b.top)-(b.height-a.height)/2;
    }
    self.repaint=repaint; self.focusCurrent=focusCurrent; self.data=data||null;

    var raf=0;
    input.addEventListener('input',function(){
      clr.hidden=!input.value;
      if(raf) return;
      raf=requestAnimationFrame(function(){ raf=0; repaint(); });
    });
    input.addEventListener('keydown',function(e){
      if(e.key!=='Escape') return;
      e.stopPropagation();                                    // 别让外层把它当成"关面板"
      if(input.value){ input.value=''; clr.hidden=true; repaint(''); }
      else closeSheet();
    });
    clr.addEventListener('click',function(){ input.value=''; clr.hidden=true; repaint(''); input.focus(); });

    scroll.addEventListener('click',function(ev){
      var b=ev.target && ev.target.closest ? ev.target.closest('.tm-item') : null;
      if(!b || b.disabled || b.__busy) return;
      b.__busy=true; b.classList.add('tm-busy');
      var p=b.getAttribute('data-p'), mid=b.getAttribute('data-m');
      fetch('/api/task-model',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({topic_id:id,provider:p,model:mid})})
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
        .then(function(res){
          if(!res.ok || !res.d || !res.d.success) throw Error((res.d&&res.d.error)||'选择失败');
          delete cacheMap[id]; shown='';                        // 失效：下次重取，保证"当前"标记正确
          closeSheet();
          if(tid()===id) load(true);
        })
        .catch(function(err){
          b.__busy=false; b.classList.remove('tm-busy');
          alert('切换失败：'+err.message);
        });
    });

    repaint();
    focusCurrent();
    return self;
  }

  function fill(data){
    if(!live || !live.cover.isConnected){ live=null; return; }
    live.data=data;
    live.repaint(null);
    live.focusCurrent();
  }

  function failSheet(){
    if(!live || !live.cover.isConnected) return;
    live.sub.textContent='加载失败';
    live.scroll.innerHTML='<div class="tm-empty">模型列表加载失败<br><button type="button" class="tm-retry">重试</button></div>';
    var rb=live.scroll.querySelector('.tm-retry');
    if(rb) rb.addEventListener('click',function(){
      if(!live) return;
      live.scroll.innerHTML='<div class="tm-empty">加载中…</div>';
      var seq=live.seq;
      load(true).then(function(d){ if(d && live && live.seq===seq) fill(d); else failSheet(); });
    });
  }

  button.onclick=function(e){
    e.preventDefault(); e.stopPropagation();
    var id=tid(); if(!id){ alert('请先选择任务'); return; }
    var seq=++openSeq;
    var has=(cacheMap[id]&&cacheMap[id].d)||null;
    makeSheet(id,has,seq);
    if(cacheFresh(id)) return;                                 // 缓存够新：直接渲染，不发请求
    load(false).then(function(d){
      if(seq!==openSeq) return;                                // 已被下一次打开取代
      if(d) fill(d); else if(!has) failSheet();
    });
  };

  /* 切任务时后台预热：等用户点开时通常已经有缓存 */
  setInterval(function(){
    var id=tid(); if(!id || id===shown) return;
    var c=cacheOf(id);
    if(c){ applyLabel(c); shown=id; }
    if(cacheFresh(id)) return;
    if(Date.now()-lastFail<5000) return;                       // 失败退避：别每 800ms 打一次
    load(true).then(function(d){ if(!d) lastFail=Date.now(); });
  },800);
})();
