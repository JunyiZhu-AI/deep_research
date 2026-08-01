#!/usr/bin/env python3
"""
render_viz.py — builds out/viz.html. MANUAL §15.2.

The output is a single self-contained file. The graph is embedded inline as a
JS constant because a browser opening file:// will refuse to fetch a sibling
JSON file, and this deliverable is opened from disk.

Usage:
    python3 render_viz.py --run-root ./runs/<slug>
    python3 render_viz.py --run-root ./runs/<slug> --out /tmp/preview.html

Design note: threat level is the only saturated color in the interface.
Everything else -- clusters, edges, chrome -- is desaturated, so the eye lands
on prior art before anything else. That is the one question this view exists
to answer.
"""

import argparse
import json
import os
import re
import sys

CDN_CYTOSCAPE = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"
CDN_LAYOUT_BASE = "https://cdnjs.cloudflare.com/ajax/libs/layout-base/2.0.1/layout-base.js"
CDN_COSE_BASE = "https://cdnjs.cloudflare.com/ajax/libs/cose-base/2.2.0/cose-base.js"
CDN_FCOSE = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape-fcose/2.2.0/cytoscape-fcose.js"

CLUSTER_HUES = ["#7E8AA8", "#8A9E86", "#A08C7D", "#8296A8", "#9B8AA0",
                "#7D9A9A", "#A2957C", "#89809C", "#7C9B88", "#A18790",
                "#8B93A6", "#96A085"]


def load_cards(root):
    out = {}
    d = os.path.join(root, "corpus", "cards")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                card = json.load(fh)
            out[card.get("id", name[:-5])] = card
        except (json.JSONDecodeError, OSError):
            pass
    return out


def human_label(cid, meta):
    """MANUAL §16.4: never ship a numeric cluster label.

    `cluster_03` tells a reader nothing. If the orchestrator has not named the
    thread, fall back to the first clause of its narrative rather than the
    index -- and make the omission visible so it gets fixed.
    """
    meta = meta or {}
    label = (meta.get("label") or "").strip()
    if label and not re.fullmatch(r"cluster[_\-]?\d+", label, re.I):
        return label
    narrative = (meta.get("narrative") or "").strip()
    if narrative:
        first = re.split(r"[.;:]", narrative)[0].strip()
        if first:
            return (first[:60] + "…") if len(first) > 60 else first
    return f"UNNAMED THREAD ({cid})"


def compact(graph, cards):
    """Fold the fields the view needs into each node; drop the rest."""
    clusters = sorted({n.get("cluster") for n in graph.get("nodes", []) if n.get("cluster")})
    hue = {c: CLUSTER_HUES[i % len(CLUSTER_HUES)] for i, c in enumerate(clusters)}
    cluster_meta = {c.get("id"): c for c in graph.get("clusters", []) or []}

    nodes = []
    for n in graph.get("nodes", []):
        card = cards.get(n["id"], {})
        bib = card.get("bib", {}) or {}
        rel = card.get("relation_to_idea", {}) or {}
        nodes.append({
            "id": n["id"],
            "label": n.get("label") or bib.get("title") or n["id"],
            "type": n.get("type", "paper"),
            "date": n.get("date") or bib.get("first_preprint_date") or "",
            "cluster": n.get("cluster") or "unclustered",
            "color": hue.get(n.get("cluster"), "#6E7585"),
            "threat": n.get("threat_level") or card.get("threat_level") or "none",
            "status": n.get("status", "core"),
            "pr": (n.get("centrality") or {}).get("pagerank", 0),
            "components": n.get("components_touched") or list(rel.get("per_component", {}).keys()),
            "authors": bib.get("authors", [])[:6],
            "venue": bib.get("venue", ""),
            "url": (n.get("provenance") or {}).get("url") or (card.get("provenance") or {}).get("url", ""),
            "depth": (card.get("provenance") or {}).get("depth", ""),
            "mechanism": card.get("mechanism", ""),
            "delta": card.get("delta_question", ""),
            "relation": rel.get("type", ""),
            "per_component": rel.get("per_component", {}),
            "evidence": rel.get("evidence", [])[:4],
        })

    edges = [{"id": f"e{i}", "source": e["source"], "target": e["target"],
              "type": e.get("type", "cites"), "status": e.get("status", "verified")}
             for i, e in enumerate(graph.get("edges", []))
             if e.get("status") != "hypothesis"]

    return {
        "meta": graph.get("meta", {}),
        "nodes": nodes,
        "edges": edges,
        "clusters": [{"id": c, "color": hue[c],
                      "label": human_label(c, cluster_meta.get(c)),
                      "narrative": (cluster_meta.get(c) or {}).get("narrative", ""),
                      "era": (cluster_meta.get(c) or {}).get("era", ""),
                      "state": (cluster_meta.get(c) or {}).get("status", "")}
                     for c in clusters],
        "components": graph.get("meta", {}).get("components", []),
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ — literature map</title>
<style>
:root{
  --ground:#16181F; --panel:#1E212B; --panel-2:#242835; --rule:#2E3340;
  --ink:#D6DAE3; --ink-dim:#858C9E; --ink-faint:#5A6172;
  --accent:#5BC8C4;
  --critical:#FF3B30; --high:#FF9F0A; --medium:#C9B458;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--ground);color:var(--ink);font-family:var(--sans)}
body{display:flex;flex-direction:column;overflow:hidden}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* ---- top bar ---- */
header{border-bottom:1px solid var(--rule);background:var(--panel);flex:none}
.bar{display:flex;align-items:center;gap:18px;padding:10px 16px;flex-wrap:wrap}
.wordmark{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--ink-dim);white-space:nowrap}
.wordmark b{color:var(--ink);font-weight:600}
.modes{display:flex;border:1px solid var(--rule);border-radius:2px;overflow:hidden}
.modes button{background:none;border:0;padding:6px 14px;font-size:12px;
  letter-spacing:.06em;color:var(--ink-dim);cursor:pointer}
.modes button[aria-pressed=true]{background:var(--accent);color:#0E1014;font-weight:600}
#search{background:var(--ground);border:1px solid var(--rule);border-radius:2px;
  padding:6px 10px;font-size:12px;width:210px}
#search::placeholder{color:var(--ink-faint)}
.count{font-family:var(--mono);font-size:11px;color:var(--ink-dim);
  font-variant-numeric:tabular-nums;margin-left:auto}

/* ---- signature: the priority ruler ---- */
.ruler{padding:0 16px 12px;position:relative}
.ruler-track{position:relative;height:34px;border-bottom:1px solid var(--rule)}
.tick{position:absolute;bottom:0;width:1px;background:var(--rule);height:6px}
.tick span{position:absolute;bottom:8px;left:-14px;width:28px;text-align:center;
  font-family:var(--mono);font-size:9px;color:var(--ink-faint)}
.pip{position:absolute;bottom:9px;width:3px;height:3px;border-radius:50%;
  background:var(--ink-faint);transform:translateX(-1px)}
.pip.t-critical,.pip.t-high{width:5px;height:5px;bottom:8px}
.pip.t-critical{background:var(--critical)}
.pip.t-high{background:var(--high)}
.marker{position:absolute;top:0;bottom:0;width:1px;background:var(--accent);display:none}
.marker::after{content:attr(data-label);position:absolute;top:-1px;left:5px;
  font-family:var(--mono);font-size:9px;color:var(--accent);white-space:nowrap}
.ruler-note{font-family:var(--mono);font-size:10px;color:var(--ink-faint);
  margin-top:5px;letter-spacing:.04em}
#year{width:100%;margin-top:8px;accent-color:var(--accent)}

/* ---- layout ---- */
main{flex:1;display:flex;min-height:0}
aside{width:212px;flex:none;border-right:1px solid var(--rule);background:var(--panel);
  overflow-y:auto;padding:14px 14px 40px}
aside h3{font-family:var(--mono);font-size:10px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--ink-faint);margin:20px 0 8px;font-weight:500}
aside h3:first-child{margin-top:0}
.row{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px;cursor:pointer}
.row input{accent-color:var(--accent);margin:0}
.sw{width:9px;height:9px;border-radius:2px;flex:none}
.row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-dim)}
.row:hover span{color:var(--ink)}
.threat-key{display:flex;align-items:center;gap:8px;font-size:11px;padding:3px 0;
  color:var(--ink-dim)}
.ring{width:10px;height:10px;border-radius:50%;border:2px solid var(--ink-faint);flex:none}

#cy{flex:1;min-width:0;background:
  radial-gradient(ellipse at 50% 40%,#1A1D26 0%,var(--ground) 70%)}

section.detail{width:330px;flex:none;border-left:1px solid var(--rule);
  background:var(--panel);overflow-y:auto;padding:16px 16px 40px}
.empty{color:var(--ink-faint);font-size:13px;line-height:1.6;margin-top:40px}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--ink-faint);margin:18px 0 6px}
.title{font-size:15px;line-height:1.35;margin:0 0 8px;font-weight:600}
.meta{font-family:var(--mono);font-size:11px;color:var(--ink-dim);line-height:1.7;
  font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-family:var(--mono);font-size:10px;padding:2px 7px;
  border-radius:2px;letter-spacing:.08em;text-transform:uppercase}
.b-critical{background:var(--critical);color:#170203;font-weight:700}
.b-high{background:var(--high);color:#1A1000;font-weight:700}
.b-medium{background:var(--medium);color:#1A1600;font-weight:600}
.b-low,.b-none{border:1px solid var(--rule);color:var(--ink-faint)}
.prose{font-size:13px;line-height:1.6;color:var(--ink-dim)}
.anchor{border-left:2px solid var(--rule);padding-left:10px;margin:6px 0;
  font-size:12px;color:var(--ink-dim);line-height:1.55}
.anchor em{color:var(--ink);font-style:normal}
a{color:var(--accent)}
.pill{display:inline-block;font-family:var(--mono);font-size:10px;border:1px solid var(--rule);
  border-radius:2px;padding:1px 6px;margin:2px 3px 2px 0;color:var(--ink-dim)}
.pill.hit{border-color:var(--accent);color:var(--accent)}

#fallback{display:none;padding:24px;overflow:auto;flex:1}
#fallback table{border-collapse:collapse;font-size:12px;width:100%}
#fallback th,#fallback td{border-bottom:1px solid var(--rule);padding:6px 10px;text-align:left}
#fallback th{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-faint)}

@media (max-width:1100px){aside{display:none}section.detail{width:270px}}
@media (max-width:760px){main{flex-direction:column}section.detail{width:auto;
  border-left:0;border-top:1px solid var(--rule);max-height:45%}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body>

<header>
  <div class="bar">
    <div class="wordmark"><b>__TITLE__</b> · literature map</div>
    <div class="modes" role="group" aria-label="View mode">
      <button data-mode="map" aria-pressed="true">Map</button>
      <button data-mode="novelty" aria-pressed="false">Novelty</button>
      <button data-mode="era" aria-pressed="false">Era</button>
    </div>
    <input id="search" type="search" placeholder="Search titles, mechanisms…"
           aria-label="Search the graph">
    <select id="component" aria-label="Highlight component" style="display:none;
      background:var(--ground);border:1px solid var(--rule);border-radius:2px;padding:6px 8px;font-size:12px"></select>
    <div class="count" id="count"></div>
  </div>
  <div class="ruler">
    <div class="ruler-track" id="track"><div class="marker" id="marker"></div></div>
    <input id="year" type="range" aria-label="Show work published up to this year">
    <div class="ruler-note" id="rulernote">Every artifact by first-preprint date. Select a node to see what predates it.</div>
  </div>
</header>

<main>
  <aside>
    <h3>Threat</h3>
    <div class="threat-key"><span class="ring" style="border-color:var(--critical)"></span>critical — anticipates the idea</div>
    <div class="threat-key"><span class="ring" style="border-color:var(--high)"></span>high — anticipates a load-bearing part</div>
    <div class="threat-key"><span class="ring" style="border-color:var(--medium)"></span>medium — partial or assumption-heavy</div>
    <div class="threat-key"><span class="ring"></span>low / none — context</div>
    <h3>Threads</h3><div id="clusters"></div>
    <h3>Relations</h3><div id="edgetypes"></div>
    <h3>Scope</h3>
    <label class="row"><input type="checkbox" id="showall"><span>Show all __NODECOUNT__ nodes</span></label>
    <label class="row"><input type="checkbox" id="periphery"><span>Include periphery</span></label>
    <p style="font-size:11px;line-height:1.5;color:var(--ink-faint);margin:10px 0 0">
      <span id="scopenote">Opens on the 60 that matter most, by threat then
      centrality. Citation links are hidden by default — they are the most
      numerous and say the least.</span></p>
  </aside>

  <div id="cy"></div>
  <div id="fallback"></div>

  <section class="detail" id="detail">
    <p class="empty">Select a node to read its digest.<br><br>
    Nodes are sized by centrality and ringed by how badly they threaten the
    idea's novelty. Colour marks the thread they belong to.</p>
  </section>
</main>

<script>const GRAPH = /*__GRAPH_DATA__*/;</script>
<script src="__CDN_CYTOSCAPE__"></script>
<script src="__CDN_LAYOUT_BASE__"></script>
<script src="__CDN_COSE_BASE__"></script>
<script src="__CDN_FCOSE__"></script>
<script>
(function(){
  "use strict";
  var THREAT_COLOR = {critical:"#FF3B30", high:"#FF9F0A", medium:"#C9B458",
                      low:"#5A6172", none:"#3A4050"};
  var THREAT_RANK = {critical:4, high:3, medium:2, low:1, none:0};
  var SHAPE = {paper:"ellipse", artifact:"round-rectangle", method:"diamond",
               benchmark:"rectangle", dataset:"rectangle", concept:"hexagon",
               problem:"hexagon", claim:"triangle", group:"pentagon"};

  var esc = function(s){ return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;"); };
  var year = function(d){ var y = parseInt(String(d).slice(0,4),10); return isNaN(y)?null:y; };

  /* ---------- the priority ruler: who was first ---------- */
  var years = GRAPH.nodes.map(function(n){return year(n.date);})
                         .filter(function(y){return y;});
  var minY = years.length ? Math.min.apply(null,years) : 2000;
  var maxY = years.length ? Math.max.apply(null,years) : 2026;
  var track = document.getElementById("track");
  var marker = document.getElementById("marker");
  var pos = function(y){ return maxY===minY ? 50 : (y-minY)/(maxY-minY)*100; };

  (function drawRuler(){
    var step = (maxY-minY) > 24 ? 5 : ((maxY-minY) > 10 ? 2 : 1);
    for(var y=Math.ceil(minY/step)*step; y<=maxY; y+=step){
      var t=document.createElement("div"); t.className="tick";
      t.style.left=pos(y)+"%"; t.innerHTML='<span>'+y+'</span>'; track.appendChild(t);
    }
    GRAPH.nodes.forEach(function(n){
      var y=year(n.date); if(!y) return;
      var p=document.createElement("div");
      p.className="pip t-"+n.threat; p.style.left=pos(y)+"%";
      p.title=n.label+" ("+y+")"; track.appendChild(p);
    });
  })();

  var slider=document.getElementById("year");
  slider.min=minY; slider.max=maxY; slider.value=maxY; slider.step=1;

  /* ---------- graph ---------- */
  var cy=null;
  if(typeof cytoscape==="undefined"){
    document.getElementById("cy").style.display="none";
    var fb=document.getElementById("fallback"); fb.style.display="block";
    fb.innerHTML='<p class="prose">The graph library could not be loaded '+
      '(no network). The corpus is listed below; reopen with a connection for '+
      'the map.</p><table><thead><tr><th>Date</th><th>Threat</th><th>Title</th>'+
      '<th>Thread</th></tr></thead><tbody>'+
      GRAPH.nodes.slice().sort(function(a,b){return (a.date||"").localeCompare(b.date||"");})
        .map(function(n){return '<tr><td>'+esc(n.date)+'</td><td>'+esc(n.threat)+
          '</td><td>'+esc(n.label)+'</td><td>'+esc(n.cluster)+'</td></tr>';}).join("")+
      '</tbody></table>';
    return;
  }
  if(typeof cytoscapeFcose!=="undefined"){ cytoscape.use(cytoscapeFcose); }
  var hasFcose = typeof cytoscapeFcose!=="undefined";

  cy=cytoscape({
    container:document.getElementById("cy"),
    elements:{
      nodes:GRAPH.nodes.map(function(n){return {data:n};}),
      edges:GRAPH.edges.map(function(e){return {data:e};})
    },
    style:[
      {selector:"node",style:{
        "background-color":"data(color)","label":"","width":"mapData(pr,0,0.05,14,52)",
        "height":"mapData(pr,0,0.05,14,52)","shape":function(e){return SHAPE[e.data("type")]||"ellipse";},
        "border-width":function(e){var t=e.data("threat");return THREAT_RANK[t]>=3?4:(THREAT_RANK[t]===2?2:0);},
        "border-color":function(e){return THREAT_COLOR[e.data("threat")]||"#3A4050";},
        "transition-property":"opacity","transition-duration":"140ms"}},
      {selector:"node.faded",style:{"opacity":0.08}},
      {selector:"node.hidden",style:{"display":"none"}},
      {selector:"node.pick",style:{"border-width":4,"border-color":"#5BC8C4",
        "label":"data(label)","font-size":10,"color":"#D6DAE3","text-wrap":"wrap",
        "text-max-width":170,"text-margin-y":-6,"text-background-color":"#16181F",
        "text-background-opacity":0.88,"text-background-padding":3,"z-index":99}},
      {selector:"node.match",style:{"label":"data(label)","font-size":9,
        "color":"#5BC8C4","text-max-width":150,"text-wrap":"ellipsis"}},
      {selector:"node.named",style:{"label":"data(label)","font-size":9,
        "color":"#9AA2B4","text-max-width":140,"text-wrap":"ellipsis",
        "text-margin-y":-4}},
      {selector:"edge",style:{"width":1,"line-color":"#2E3340","curve-style":"haystack",
        "opacity":0.5}},
      {selector:'edge[type="builds_on"]',style:{"width":2,"line-color":"#454C5E"}},
      {selector:'edge[type="contradicts"]',style:{"width":2,"line-color":"#FF3B30",
        "line-style":"dashed","opacity":0.8}},
      {selector:'edge[type="reinvents"],edge[type="subsumes"],edge[type="special_case_of"]',
        style:{"width":3,"line-color":"#FF9F0A","opacity":0.85}},
      {selector:"edge.faded",style:{"opacity":0.03}},
      {selector:"edge.hidden",style:{"display":"none"}}
    ],
    wheelSensitivity:0.2
  });

  var runLayout=function(mode){
    if(mode==="era"){
      var span=Math.max(1,maxY-minY), h=cy.height()||600, w=cy.width()||900;
      var lanes={}; GRAPH.clusters.forEach(function(c,i){lanes[c.id]=i;});
      var nlane=Math.max(1,GRAPH.clusters.length);
      cy.layout({name:"preset",fit:true,padding:40,positions:function(n){
        var y=year(n.data("date"))||minY;
        return {x:((y-minY)/span)*(w*0.86)+w*0.07,
                y:((lanes[n.data("cluster")]||0)+0.5)/nlane*(h*0.82)+h*0.09};
      }}).run();
      document.getElementById("rulernote").textContent=
        "Era view — horizontal position is the first-preprint date; each lane is one thread.";
    } else {
      cy.layout(hasFcose
        ? {name:"fcose",quality:"proof",randomize:mode!=="map",animate:false,
           nodeSeparation:90,idealEdgeLength:100,nodeRepulsion:9000,padding:40}
        : {name:"cose",animate:false,padding:40}).run();
      document.getElementById("rulernote").textContent=
        "Every artifact by first-preprint date. Select a node to see what predates it.";
    }
  };
  runLayout("map");

  /* ---------- filters ---------- */
  var state={mode:"map",cluster:{},edge:{},periphery:false,q:"",year:maxY,component:"",
             showAll:false};
  GRAPH.clusters.forEach(function(c){state.cluster[c.id]=true;});
  var edgeTypes=Array.from(new Set(GRAPH.edges.map(function(e){return e.type;}))).sort();
  // MANUAL §16.4: citation edges are the most numerous and least informative
  // relation. Open on the edges that carry meaning; cites is a toggle.
  edgeTypes.forEach(function(t){state.edge[t]= t!=="cites";});

  // Degenerate case: if the graph is almost entirely citations, hiding them
  // leaves a node cloud with no visible structure -- less readable, not more.
  // Turn them back on and say why, so the reader is not looking at an
  // unexplained void.
  var semantic=GRAPH.edges.filter(function(e){return e.type!=="cites";}).length;
  var citesRestored=false;
  if(semantic < 20){ state.edge["cites"]=true; citesRestored=true; }

  if(citesRestored){
    document.getElementById("scopenote").textContent=
      "Opens on the 60 that matter most, by threat then centrality. This graph "+
      "has few typed relations, so citation links are shown — without them "+
      "there would be almost no structure to read.";
  }

  var host=document.getElementById("clusters");
  GRAPH.clusters.forEach(function(c){
    var l=document.createElement("label"); l.className="row";
    l.innerHTML='<input type="checkbox" checked><span class="sw" style="background:'+c.color+'"></span>'+
                '<span title="'+esc(c.narrative||c.label)+'">'+esc(c.label)+'</span>';
    l.querySelector("input").addEventListener("change",function(e){
      state.cluster[c.id]=e.target.checked; apply();});
    host.appendChild(l);
  });
  var ehost=document.getElementById("edgetypes");
  edgeTypes.forEach(function(t){
    var l=document.createElement("label"); l.className="row";
    l.innerHTML='<input type="checkbox" checked><span>'+esc(t.replace(/_/g," "))+'</span>';
    l.querySelector("input").addEventListener("change",function(e){
      state.edge[t]=e.target.checked; apply();});
    ehost.appendChild(l);
  });

  var sel=document.getElementById("component");
  sel.innerHTML='<option value="">All components</option>'+
    (GRAPH.components||[]).map(function(c){return '<option value="'+esc(c)+'">'+esc(c)+'</option>';}).join("");
  sel.addEventListener("change",function(e){state.component=e.target.value; apply();});

  document.getElementById("periphery").addEventListener("change",function(e){
    state.periphery=e.target.checked; apply();});
  document.getElementById("showall").addEventListener("change",function(e){
    state.showAll=e.target.checked; runLayout(state.mode); apply();});
  document.getElementById("search").addEventListener("input",function(e){
    state.q=e.target.value.toLowerCase().trim(); apply();});
  slider.addEventListener("input",function(e){state.year=parseInt(e.target.value,10); apply();});

  Array.prototype.forEach.call(document.querySelectorAll(".modes button"),function(b){
    b.addEventListener("click",function(){
      Array.prototype.forEach.call(document.querySelectorAll(".modes button"),
        function(x){x.setAttribute("aria-pressed", x===b ? "true":"false");});
      state.mode=b.dataset.mode;
      sel.style.display = state.mode==="novelty" ? "" : "none";
      runLayout(state.mode); apply();
    });
  });

  // MANUAL §16.4: a 240-node hairball is a picture of effort, not a
  // communication. Rank by threat, then centrality, and open on the top 60.
  var RANKED=GRAPH.nodes.slice().sort(function(a,b){
    var d=(THREAT_RANK[b.threat]||0)-(THREAT_RANK[a.threat]||0);
    return d!==0?d:(b.pr||0)-(a.pr||0);
  });
  var DEFAULT_SET={}; RANKED.slice(0,60).forEach(function(n){DEFAULT_SET[n.id]=1;});
  var LABEL_SET={};   RANKED.slice(0,15).forEach(function(n){LABEL_SET[n.id]=1;});
  var overflow=GRAPH.nodes.length-Object.keys(DEFAULT_SET).length;

  function apply(){
    var shown=0;
    cy.batch(function(){
      cy.nodes().forEach(function(n){
        var d=n.data(), y=year(d.date);
        var hide = !state.cluster[d.cluster]
                || (y && y>state.year)
                || (d.status==="periphery" && !state.periphery)
                || (!state.showAll && !DEFAULT_SET[d.id]);
        n.toggleClass("hidden",hide);
        if(!hide) shown++;
        var q=state.q, hit = q && ((d.label||"").toLowerCase().indexOf(q)>=0 ||
                                   (d.mechanism||"").toLowerCase().indexOf(q)>=0 ||
                                   (d.id||"").toLowerCase().indexOf(q)>=0);
        n.toggleClass("match", !!hit);
        n.toggleClass("named", !!LABEL_SET[d.id] && !hide);
        var dim = (q && !hit) ||
          (state.mode==="novelty" && state.component &&
           (d.components||[]).indexOf(state.component)<0);
        n.toggleClass("faded", !!dim && !hide);
      });
      cy.edges().forEach(function(e){
        var d=e.data();
        var hide = !state.edge[d.type] || e.source().hasClass("hidden") || e.target().hasClass("hidden");
        e.toggleClass("hidden",hide);
        e.toggleClass("faded", !hide && (e.source().hasClass("faded")||e.target().hasClass("faded")));
      });
    });
    document.getElementById("count").textContent =
      shown+" / "+GRAPH.nodes.length+" nodes · "+GRAPH.edges.length+" relations"+
      (!state.showAll&&overflow>0 ? "  ·  "+overflow+" more hidden" : "");
  }
  apply();

  /* ---------- detail panel ---------- */
  var panel=document.getElementById("detail");
  cy.on("tap","node",function(evt){
    var n=evt.target; cy.nodes().removeClass("pick"); n.addClass("pick");
    var d=n.data(), y=year(d.date);
    if(y){
      marker.style.display="block"; marker.style.left=pos(y)+"%";
      var earlier=GRAPH.nodes.filter(function(o){var oy=year(o.date);return oy&&oy<y;}).length;
      marker.dataset.label=y;
      document.getElementById("rulernote").textContent=
        earlier+" of "+GRAPH.nodes.length+" artifacts predate this one.";
    }
    var cluster=(GRAPH.clusters.filter(function(c){return c.id===d.cluster;})[0]||{});
    var comps=Object.keys(d.per_component||{}).map(function(k){
      var v=d.per_component[k];
      return '<span class="pill'+(v==="anticipated"?" hit":"")+'">'+esc(k)+": "+esc(v)+'</span>';
    }).join("");
    panel.innerHTML =
      '<span class="badge b-'+esc(d.threat)+'">'+esc(d.threat)+' threat</span>'+
      '<p class="title" style="margin-top:10px">'+esc(d.label)+'</p>'+
      '<div class="meta">'+esc((d.authors||[]).join(", "))+
        (d.authors&&d.authors.length>=6?" et al.":"")+'<br>'+
        esc(d.date||"date unknown")+(d.venue?" · "+esc(d.venue):"")+'<br>'+
        esc(d.id)+(d.depth?" · "+esc(d.depth):"")+'</div>'+
      (d.url?'<p class="meta"><a href="'+esc(d.url)+'" target="_blank" rel="noopener">Open source ↗</a></p>':"")+
      (cluster.label?'<div class="eyebrow">Thread</div><p class="prose">'+
        esc(cluster.label)+(cluster.narrative?" — "+esc(cluster.narrative):"")+'</p>':"")+
      (d.mechanism?'<div class="eyebrow">Mechanism</div><p class="prose">'+esc(d.mechanism)+'</p>':"")+
      (d.relation?'<div class="eyebrow">Relation to the idea</div><p class="prose">'+
        esc(d.relation.replace(/_/g," "))+'</p>':"")+
      (comps?'<div class="eyebrow">Components</div><div>'+comps+'</div>':"")+
      (d.delta?'<div class="eyebrow">What remains novel</div><p class="prose">'+esc(d.delta)+'</p>':"")+
      ((d.evidence||[]).length?'<div class="eyebrow">Evidence</div>'+
        d.evidence.map(function(e){return '<p class="anchor">§'+esc(e.section||"?")+
          ' — <em>'+esc(e.anchor||"")+'</em>'+(e.note?"<br>"+esc(e.note):"")+'</p>';}).join(""):"");
    panel.scrollTop=0;
  });
  cy.on("tap",function(e){ if(e.target===cy){
    cy.nodes().removeClass("pick"); marker.style.display="none";
    panel.innerHTML='<p class="empty">Select a node to read its digest.</p>';
  }});
})();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = args.run_root
    src = os.path.join(root, "out", "graph.json")
    if not os.path.exists(src):
        src = os.path.join(root, "graph", "graph.json")
    if not os.path.exists(src):
        sys.exit(f"[render_viz] no graph found under {root}")

    with open(src, encoding="utf-8") as fh:
        graph = json.load(fh)

    payload = compact(graph, load_cards(root))
    title = graph.get("meta", {}).get("idea_slug") or os.path.basename(os.path.abspath(root))

    html = (TEMPLATE
            .replace("/*__GRAPH_DATA__*/",
                     json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            .replace("__TITLE__", title.replace("<", "").replace(">", ""))
            .replace("__NODECOUNT__", str(len(payload["nodes"])))
            .replace("__CDN_CYTOSCAPE__", CDN_CYTOSCAPE)
            .replace("__CDN_LAYOUT_BASE__", CDN_LAYOUT_BASE)
            .replace("__CDN_COSE_BASE__", CDN_COSE_BASE)
            .replace("__CDN_FCOSE__", CDN_FCOSE))

    out = args.out or os.path.join(root, "out", "viz.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)

    unnamed = [c["label"] for c in payload["clusters"] if c["label"].startswith("UNNAMED")]
    if unnamed:
        print(f"[render_viz] WARNING: {len(unnamed)} thread(s) have no human name "
              f"(MANUAL §16.4). Name them before delivery.", file=sys.stderr)
    print(f"[render_viz] wrote {out}  "
          f"({len(payload['nodes'])} nodes, {len(payload['edges'])} edges, "
          f"{len(html)/1024:.0f} KB, self-contained)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
