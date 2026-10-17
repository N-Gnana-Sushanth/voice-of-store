const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nice = s => String(s||"").replaceAll("_"," ").replace(/\b\w/g,c=>c.toUpperCase());
async function api(url, options={}) {
  const res = await fetch(url, options);
  const data = await res.json().catch(()=>({error:"Unexpected server response"}));
  if(!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.remove("hidden");setTimeout(()=>t.classList.add("hidden"),2600)}
function showPage(name){
  $$(".page").forEach(p=>p.classList.toggle("visible",p.id===name));
  $$(".nav").forEach(b=>b.classList.toggle("active",b.dataset.page===name));
  $("#page-title").textContent=({"overview":"Overview","signals":"FIRST SIGNAL","feedback":"All Feedback","actions":"Action Tracker","reports":"Weekly Reports"})[name]||"Overview";
  ({overview:loadOverview,signals:loadSignals,feedback:loadFeedback,actions:loadActions,reports:loadReport}[name]||loadOverview)();
}
function sortIncidents(items, mode){
 const rank={High:0,Medium:1,Low:2};
 return [...items].sort((a,b)=>{
   if(mode==="low") return (rank[b.severity]??3)-(rank[a.severity]??3) || (b.report_count||0)-(a.report_count||0);
   if(mode==="evidence") return (b.report_count||0)-(a.report_count||0) || (rank[a.severity]??3)-(rank[b.severity]??3);
   if(mode==="newest") return String(b.latest_seen||"").localeCompare(String(a.latest_seen||""));
   return (rank[a.severity]??3)-(rank[b.severity]??3) || (b.report_count||0)-(a.report_count||0);
 });
}
function filterIncidents(items, prefix){
 const q=($(`#${prefix}-search`)?.value||"").trim().toLowerCase();
 const sev=$(`#${prefix}-severity`)?.value||"";
 const status=$(`#${prefix}-status`)?.value||"";
 return items.filter(i=>{
  const hay=[i.summary,i.store_id,i.issue_type,i.department,i.assigned_user,i.id].join(" ").toLowerCase();
  return (!q||hay.includes(q))&&(!sev||i.severity===sev)&&(!status||i.status===status);
 });
}
function incidentCard(i, actions=true){
  const evidence=(i.evidence||[]).slice(0,3).map(e=>`<div class="quote">“${esc(e.original_text)}” <span class="incident-meta">— ${esc(e.id)} · ${esc(e.channel)}</span></div>`).join("");
  const statusOpts=["Detected — awaiting review","Under investigation","Confirmed","Inconclusive","Not substantiated","Action in progress","Pending verification","Resolved"];
  return `<article class="incident-card" data-id="${esc(i.id)}">
    <div class="incident-top"><div><div class="incident-title">${esc(i.summary)} <span class="incident-meta">· ${esc(i.id)}</span></div>
    <div class="incident-meta">${esc(i.store_id)} · ${nice(i.issue_type)} · ${i.report_count} linked report(s) · First ${esc(i.first_seen||"—")}</div></div><span class="pill ${esc(i.severity)}">${esc(i.severity)} priority</span></div>
    <div class="why"><b>Why raised:</b> ${esc(i.why_raised||"Related feedback requires review.")}<br><b>Suggested owner:</b> ${esc(i.department||"Unassigned")} · <b>Latest:</b> ${esc(i.latest_seen||"—")}
    ${i.kind?`<br><b>Detection:</b> ${i.kind==="safety"?"Safety bypass (immediate)":i.kind==="emerging"?"Emerging-issue detector (statistical)":esc(i.kind)}`:""}
    ${i.channel_counts&&Object.keys(i.channel_counts).length?`<br><b>Channels:</b> ${Object.entries(i.channel_counts).map(([k,v])=>esc(k)+" ×"+v).join(", ")}${i.duplicate_reports_linked?` · ${i.unique_reports} unique, ${i.duplicate_reports_linked} linked duplicate(s)`:""}`:""}
    ${i.escalation_rule?`<br><span class="incident-meta"><b>Escalation rule:</b> ${esc(i.escalation_rule)}</span>`:""}</div>
    ${evidence || `<p class="muted">No linked evidence yet. Original feedback remains available in All Feedback.</p>`}
    <div class="view-row"><button class="quiet view-problem" data-id="${esc(i.id)}">View full problem statement →</button></div>
    ${actions?`<div class="actions">
      <select class="status-select" aria-label="Incident status">${statusOpts.map(s=>`<option ${s===i.status?"selected":""}>${esc(s)}</option>`).join("")}</select>
      <input class="owner-input" placeholder="Assigned user" value="${esc(i.assigned_user||"")}" aria-label="Assigned user">
      <select class="department-select" aria-label="Department">${["Store Ops","Supply Chain","Merchandising","Digital","HR","Unassigned"].map(s=>`<option ${s===i.department?"selected":""}>${s}</option>`).join("")}</select>
      <button class="primary save-incident">Save update</button>
      <button class="quick-action" data-quick-status="Under investigation">Acknowledge</button><button class="quick-action" data-quick-status="Resolved">Resolve</button>
      <input class="note-input" placeholder="Investigation note / resolution" aria-label="Investigation note">
    </div>`:""}
  </article>`;
}
async function loadOverview(){
 try{const d=await api("/api/overview");$("#feedback-count").textContent=d.feedback_count;$("#open-count").textContent=d.unresolved_count;$("#high-count").textContent=d.high_priority_count;
 $("#overview-incidents").innerHTML=d.incidents.length?d.incidents.map(i=>incidentCard(i,false)).join(""):`<div class="empty">Nothing needs attention yet. Add or import feedback to begin.</div>`;
 }catch(e){toast(e.message)}
}
async function loadSignals(){
 try{const d=filterIncidents(sortIncidents(await api("/api/incidents"), $("#signal-sort")?.value||"high"),"signal");$("#signal-list").innerHTML=d.length?`<p class="muted signal-count">Showing ${d.length} problem statement(s). Open a statement to review linked evidence and its audit history.</p>`+d.map(i=>incidentCard(i)).join(""):`<div class="empty">No signals yet. Import feedback or add reports. Ordinary issues need repeated local evidence; mandatory-risk reports can surface individually.</div>`;
 }catch(e){toast(e.message)}
}
async function loadActions(){
 try{const d=filterIncidents(sortIncidents(await api("/api/incidents"), $("#action-sort")?.value||"high"),"action");$("#action-list").innerHTML=d.length?d.map(i=>incidentCard(i)).join(""):`<div class="empty">No actions are waiting. New incidents will appear here.</div>`;
 }catch(e){toast(e.message)}
}
async function loadFeedback(){
 const params=new URLSearchParams();if($("#feedback-search").value)params.set("q",$("#feedback-search").value);if($("#store-filter").value)params.set("store_id",$("#store-filter").value);if($("#issue-filter").value)params.set("issue_type",$("#issue-filter").value);
 try{const d=await api("/api/feedback?"+params);$("#feedback-rows").innerHTML=d.length?d.map(r=>`<tr><td>${esc((r.timestamp||"").replace("T"," ").slice(0,16))}</td><td>${esc(r.store_id)}</td><td>${esc(r.channel)}</td><td>${esc(nice(r.issue_type))}</td><td><span class="pill ${esc(r.severity)}">${esc(r.severity)}</span></td><td>${esc(r.original_text)}</td></tr>`).join(""):`<tr><td colspan="6">No matching feedback.</td></tr>`;
 const issues=[...new Set(d.map(r=>r.issue_type))];const sel=$("#issue-filter"),prev=sel.value;sel.innerHTML='<option value="">All issue types</option>'+issues.map(i=>`<option value="${esc(i)}">${esc(nice(i))}</option>`).join("");sel.value=prev;
 }catch(e){toast(e.message)}
}
async function loadRegions(){
 try{const d=await api("/api/regions");$("#region-links").innerHTML=d.regions.length?`<b>Printable one-page regional briefs</b> (data as of ${esc(String(d.as_of).slice(0,10))}): `+d.regions.map(r=>`<a href="/brief/${encodeURIComponent(r.region)}" target="_blank">${esc(r.region)}</a>`).join(" · "):`No regions yet. Import a store master via /api/stores/import.`}catch(e){$("#region-links").textContent="Regional briefs unavailable: "+e.message}
}
async function loadReport(){
 loadRegions();
 try{const d=await api("/api/reports/weekly");$("#report-meta").innerHTML=`<b>Period:</b> ${esc(d.period.start.slice(0,10))} to ${esc(d.period.end.slice(0,10))} · <b>${d.feedback_count}</b> feedback records · ${esc(d.basis)}`;
 $("#report-list").innerHTML=d.top_issues.length?d.top_issues.map(i=>`<article class="report-item"><h3>${esc(nice(i.issue_type))} <span class="pill">${i.count} reports</span></h3><p class="muted">Owner: ${esc(i.department)}</p><div class="report-grid"><div><b>Stores</b>${esc(i.stores.join(", ")||"—")}</div><div><b>Next action</b>${esc(i.recommended_action)}</div><div><b>Evidence</b>${i.evidence.map(e=>`“${esc(e.quote)}” (${esc(e.store_id)})`).join("<br>")||"No evidence in period"}</div></div></article>`).join(""):`<div class="empty">No feedback in this reporting window. Import a dataset or adjust timestamps.</div>`;
 $("#report-high").innerHTML=`<h2 style="margin:24px 0 10px">High-severity feedback</h2>`+(d.high_severity_evidence.length?d.high_severity_evidence.map(x=>`<div class="quote"><b>${esc(x.store_id)} · ${esc(nice(x.issue_type))}</b>: “${esc(x.quote)}”</div>`).join(""):`<p class="muted">No high-severity feedback in this period.</p>`);
 }catch(e){toast(e.message)}
}
document.addEventListener("click",async e=>{
 const nav=e.target.closest("[data-page]");if(nav)showPage(nav.dataset.page);
 const go=e.target.closest("[data-goto]");if(go)showPage(go.dataset.goto);
 if(e.target.id==="open-ingest")$("#modal").classList.remove("hidden");
 if(e.target.id==="close-modal"||e.target.id==="modal")$("#modal").classList.add("hidden");
 if(e.target.id==="refresh-signals"){await loadSignals();toast("Signal scan refreshed")}
 if(e.target.id==="close-detail"||e.target.id==="detail-modal")$("#detail-modal").classList.add("hidden");
 const view=e.target.closest(".view-problem"); if(view){const id=view.dataset.id;try{const all=await api("/api/incidents");const i=all.find(x=>x.id===id);if(!i)throw new Error("Problem statement not found");await api("/api/incidents/"+encodeURIComponent(id)+"/view",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({actor:"Reviewer"})});const ev=(i.evidence||[]).map(x=>`<div class="quote"><b>${esc(x.id)} · ${esc(x.store_id||i.store_id)} · ${esc(x.timestamp||"")}</b><p>${esc(x.original_text)}</p><span class="incident-meta">${esc(x.channel)} · ${esc(nice(x.issue_type))} · ${esc(x.severity)}</span></div>`).join("");$("#detail-title").textContent=i.summary||"Problem statement";$("#detail-content").innerHTML=`<div class="detail-meta"><span class="pill ${esc(i.severity)}">${esc(i.severity)} priority</span> <span class="pill">${esc(i.status||"Detected")}</span></div><p><b>Problem statement</b></p><p>${esc(i.summary||"No summary available.")}</p><p><b>Why this was raised</b><br>${esc(i.why_raised||"Related feedback requires review.")}</p><p><b>Store / issue type</b><br>${esc(i.store_id)} · ${esc(nice(i.issue_type))}</p><p><b>Linked reports:</b> ${i.report_count||0} · <b>First seen:</b> ${esc(i.first_seen||"—")} · <b>Latest:</b> ${esc(i.latest_seen||"—")}</p><p><b>Suggested owner:</b> ${esc(i.department||"Unassigned")} · <b>Assigned to:</b> ${esc(i.assigned_user||"Not assigned")}</p><h3>Original evidence (${(i.evidence||[]).length})</h3>${ev||"<p class=muted>No linked evidence yet.</p>"}<h3>Investigation history</h3>${(i.audit||[]).length?i.audit.map(a=>`<div class="audit-row"><b>${esc(a.action)}</b> · ${esc(a.timestamp)}<p>${esc(a.note||"")}</p></div>`).join(""):"<p class=muted>No investigation updates recorded yet.</p>"}`;$("#detail-modal").classList.remove("hidden");toast("Problem statement view recorded");}catch(err){toast(err.message)}}
 const quick=e.target.closest(".quick-action");if(quick){const card=quick.closest(".incident-card");const id=card.dataset.id;const next=quick.dataset.quickStatus;try{await api("/api/incidents/"+encodeURIComponent(id),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:next,assigned_user:card.querySelector(".owner-input")?.value||"",department:card.querySelector(".department-select")?.value||"Unassigned",note:next==="Resolved"?"Marked resolved from quick action":"Acknowledged from quick action"})});toast(next==="Resolved"?"Incident marked resolved":"Incident acknowledged");loadSignals();loadActions();loadOverview();}catch(err){toast(err.message)}return;}
 const save=e.target.closest(".save-incident");if(save){const card=save.closest(".incident-card");const id=card.dataset.id;
  const note=card.querySelector(".note-input").value;
  try{await api("/api/incidents/"+encodeURIComponent(id),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:card.querySelector(".status-select").value,assigned_user:card.querySelector(".owner-input").value,department:card.querySelector(".department-select").value,resolution:note||undefined,note:note||"Workflow update"})});toast("Incident updated and audit recorded");loadSignals();}catch(err){toast(err.message)}
 }
});
$("#search-feedback").addEventListener("click",loadFeedback);
$("#feedback-form").addEventListener("submit",async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));try{const r=await api("/api/feedback",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)});$("#form-message").textContent=r.message; e.target.reset();toast("Feedback saved");loadOverview();}catch(err){$("#form-message").textContent=err.message}});
$("#import-form").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.target);try{const r=await api("/api/feedback/import",{method:"POST",body:fd});$("#import-message").textContent=`Inserted ${r.inserted}; duplicates ${r.duplicates}; invalid ${r.invalid}.`;if(r.errors.length)$("#import-message").textContent+=" First error: "+r.errors[0].error;toast("Import processed");loadOverview();}catch(err){$("#import-message").textContent=err.message}});
showPage("overview");

["signal-sort","action-sort"].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener("change",()=>id==="signal-sort"?loadSignals():loadActions())});
["signal-search","signal-severity","signal-status"].forEach(id=>document.getElementById(id)?.addEventListener(id.endsWith("search")?"input":"change",loadSignals));
["action-search","action-severity","action-status"].forEach(id=>document.getElementById(id)?.addEventListener(id.endsWith("search")?"input":"change",loadActions));
