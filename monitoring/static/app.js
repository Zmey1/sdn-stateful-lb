/* React 16 (bundled offline) UI for the actual Mininet/OVS runtime. No fake values. */
(function () {
  'use strict';
  const h = React.createElement;
  const names = ['b1', 'b2', 'b3'];
  const swatches = ['#45d1ae', '#61aafa', '#af8bff'];
  const num = (n, digit) => Number.isFinite(Number(n)) ? Number(n).toFixed(digit == null ? 1 : digit) : '—';
  const int = n => Number.isFinite(Number(n)) ? Number(n).toLocaleString() : '—';
  const badge = (label, cls) => h('span', {className:'badge ' + (cls || '')}, label);
  const sectionTitle = (eyebrow, title, aside) => h('div', {className:'section-head'},
    h('div', null, h('div', {className:'eyebrow'}, eyebrow), h('h2', null, title)), aside || null);
  const icon = (s) => h('span', {className:'icon'}, s);
  function metric(label, value, meta, cls) { return h('div', {className:'metric-card ' + (cls || '')},
    h('div',{className:'metric-label'},label), h('div',{className:'metric-value'},value), h('div',{className:'metric-caption'},meta)); }
  function progress(pct, color) {
    return h('div', {className:'progress'}, h('div', {className:'progress-fill', style:{width:Math.max(0,Math.min(100,pct || 0))+'%', background:color}}));
  }
  function Sparkline(props) {
    const data = props.points || [];
    const w=680,H=220,p=18;
    const max = Math.max(props.minimum || 1, ...data.map(x=>Number(x)||0))*1.16;
    const pts = data.map((d,i) => (p + i*(w-2*p)/Math.max(1,data.length-1)).toFixed(1)+','+(H-p-(Number(d)||0)/max*(H-2*p)).toFixed(1)).join(' ');
    const baseline=H-p;
    return h('div', {className:'chart-wrap'},
      h('div',{className:'chart-grid'},h('span',null,num(max,0)+' rps'),h('span',null,num(max/2,0)+' rps'),h('span',null,'0 rps')),
      h('svg', {viewBox:`0 0 ${w} ${H}`, role:'img', 'aria-label':'Measured requests per second over recent dashboard polling intervals'},
        [0,.5,1].map((v,i)=>h('line',{key:i,x1:p,y1:p+(H-2*p)*v,x2:w-p,y2:p+(H-2*p)*v,stroke:'#283247',strokeDasharray:'4 6'})),
        data.length > 1 ? h('polygon',{points:p+','+baseline+' '+pts+' '+(w-p)+','+baseline,fill:'rgba(69,209,174,.09)'}) : null,
        h('polyline',{fill:'none',stroke:'#45d1ae',strokeWidth:'3',strokeLinejoin:'round',strokeLinecap:'round',points:pts})),
      h('div',{className:'chart-axis'},h('span',null,'Earlier'),h('span',null,'Live · 2s refresh')));
  }
  function TrafficSplit(props) {
    const backends = props.backends || [];
    return h('div',{className:'traffic-stack'},
      h('div',{className:'split-bar'},backends.map((b,i)=>h('div',{key:b.name,style:{width:(b.window_share_pct || 0)+'%',background:swatches[i]},title:b.name+' '+b.window_share_pct+'%'}))),
      h('div',{className:'split-legend'},backends.map((b,i)=>h('div',{className:'legend-row',key:b.name},
        h('span',{className:'legend-dot',style:{background:swatches[i]}}),
        h('span',{className:'legend-name'},b.name.toUpperCase()),
        h('strong',null,num(b.window_share_pct,1)+'%'),
        h('span',{className:'legend-sub'},num(b.rps,1)+' rps')))));
  }
  function NetworkTopology(props) {
    const servers=props.backends||[];
    const node=(label,subtitle,className)=>h('div',{className:'network-node '+className},h('strong',null,label),h('small',null,subtitle));
    return h('div',{className:'network-visual'},
      h('div',{className:'topo-top'},node('Clients','h1 · h2 · h3','clients'),h('div',{className:'network-link'},h('span',null,'VIP '+(props.vip||''))),
        node('Open vSwitch','Group 100 · CT zone 10','switch')),
      h('div',{className:'network-branch'},h('div',{className:'branch-wire'}),
        servers.map((b,i)=>h('div',{className:'branch',key:b.name},
          h('div',{className:'branch-line '+ (b.state === 'DOWN' ? 'down' : '')}),
          h('div',{className:'network-node backend-node'},
            h('span',{className:'node-dot',style:{background:b.state==='DOWN'?'#ff8496':swatches[i]}}),
            h('strong',null,b.name.toUpperCase()),h('small',null,b.ip),
            h('div',{className:'network-weight'},'W '+b.weight))))),
      h('div',{className:'network-footnote'},'New connections → SELECT group · Existing connections → conntrack NAT'));
  }
  function LatencyChart(props) {
    // Client-measured p95 per second for the latest run (solid) and the run
    // before it (dashed), so two policies can be compared on one scenario.
    const runs=(props.runs||[]).filter(r=>r.points&&r.points.length);
    if(!runs.length)return h('div',{className:'empty'},'No traffic run yet. Run one of the guided steps: each second of client-measured latency appears here.');
    const w=680,H=220,p=18;
    const maxT=Math.max(...runs.map(r=>Math.max(r.meta.duration||0,...r.points.map(x=>x.t+1))));
    const maxY=Math.max(200,...runs.flatMap(r=>r.points.map(x=>Number(x.p95_ms)||0)))*1.1;
    const X=t=>p+t*(w-2*p)/Math.max(1,maxT), Y=v=>H-p-v/maxY*(H-2*p);
    const line=r=>r.points.filter(x=>x.p95_ms!=null).map(x=>X(x.t+.5).toFixed(1)+','+Y(x.p95_ms).toFixed(1)).join(' ');
    const label=r=>(r.meta.mode||'?').toUpperCase()+' · '+(r.meta.scenario==='gray-b2'?'b2 slowed mid-run':'steady')+' · '+(r.meta.rate||'?')+' req/s';
    const win=runs[0].meta.slow_window;
    const failed=r=>r.points.reduce((n,x)=>n+(x.failed||0),0);
    return h('div',{className:'chart-wrap'},
      h('div',{className:'chart-grid'},h('span',null,num(maxY,0)+' ms'),h('span',null,num(maxY/2,0)+' ms'),h('span',null,'0 ms')),
      h('svg',{viewBox:`0 0 ${w} ${H}`,role:'img','aria-label':'Client-measured p95 latency per second'},
        win?h('rect',{x:X(win[0]),y:p,width:X(win[1])-X(win[0]),height:H-2*p,fill:'rgba(255,132,150,.10)'}):null,
        win?h('text',{x:X(win[0])+6,y:p+14,fill:'#ff9cad',fontSize:12},'b2 slowed'):null,
        [0,.5,1].map((v,i)=>h('line',{key:i,x1:p,y1:p+(H-2*p)*v,x2:w-p,y2:p+(H-2*p)*v,stroke:'#283247',strokeDasharray:'4 6'})),
        runs[1]?h('polyline',{fill:'none',stroke:'#8a96ab',strokeWidth:'2',strokeDasharray:'6 5',points:line(runs[1])}):null,
        h('polyline',{fill:'none',stroke:'#45d1ae',strokeWidth:'3',strokeLinejoin:'round',points:line(runs[0])}),
        runs[0].points.filter(x=>x.failed).map(x=>h('circle',{key:x.t,cx:X(x.t+.5),cy:H-p,r:4,fill:'#ff8496'}))),
      h('div',{className:'chart-axis'},h('span',null,'0 s'),h('span',null,num(maxT,0)+' s')),
      h('div',{className:'split-legend'},runs.map((r,i)=>h('div',{className:'legend-row',key:i},
        h('span',{className:'legend-dot',style:{background:i?'#8a96ab':'#45d1ae'}}),
        h('span',null,(i?'Previous run: ':'This run: ')+label(r)),
        h('strong',null,failed(r)+' failed')))));
  }
  function BenchmarkTable(props) {
    const data=props.data||{};
    const rows=data.summary||[];
    return h('div',{className:'benchmark-wrap'},
      rows.length ? h('div',null,
        h('div',{className:'table-scroll'},h('table',null,
          h('thead',null,h('tr',null,['Scenario','Policy','Runs','P99','P95','Failures','Peak load','Throughput'].map(x=>h('th',{key:x},x)))),
          h('tbody',null,rows.map((r,i)=>h('tr',{key:i},
            h('td',null,r.scenario), h('td',null,badge(r.mode,r.mode==='adaptive'?'success':'')),
            h('td',null,int(r.runs)),h('td',null,h('strong',null,num(r.p99_ms,1)+' ms')),
            h('td',null,num(r.p95_ms,1)+' ms'),h('td',null,num(r.failure_rate_pct,2)+'%'),
            h('td',null,r.peak_load==null||r.peak_load===''?'—':num(100*r.peak_load,0)+'%'),
            h('td',null,num(r.throughput_rps,1))))))),
        h('div',{className:'hint'},'P99 / P95 are measured end-to-end by the clients. Peak load is the highest load per worker any backend reached; above 100 % it was overloaded and queueing.'))
        : h('div',{className:'empty'},'No completed benchmark summary found. Pass --bench-root with an earlier benchmark results directory, or run the benchmark suite.'),
      data.available ? h('div',{className:'path-label'},data.directory) : null);
  }
  function friendlyEvent(line) {
    if (!line) return 'Controller event';
    if (line.includes('reconciled installed weights')) return 'Controller reconnected and restored switch weights';
    if (line.includes('Reconstructed health state')) return 'Health state initialized from the switch';
    if (line.startsWith('weights=')) {
      const states = line.match(/states=\{([^}]+)\}/);
      const unhealthy = states && states[1].match(/'(b[123])': '(DOWN|RECOVERING|DRAINING)'/g);
      return unhealthy && unhealthy.length ? 'Policy update · ' + unhealthy.map(x => x.replaceAll("'", '')).join(' · ') : 'Policy update · server weights checked';
    }
    return line.length > 115 ? line.slice(0, 112) + '…' : line;
  }
  class App extends React.Component {
    constructor(props) {
      super(props);
      this.state={snapshot:null,benchmark:null,notice:'',fault:'',editingBackend:'b1',delay:100,rate:150,duration:15,busy:false,showAdvanced:false};
      this.timer=null;
    }
    componentDidMount() {
      this.refresh();
      this.timer=setInterval(()=>this.refresh(),2000);
      this.loadBench();
      this.benchTimer=setInterval(()=>this.loadBench(),20000);
    }
    componentWillUnmount(){clearInterval(this.timer);clearInterval(this.benchTimer);}
    async refresh() {
      try {
        const response=await fetch('/api/snapshot',{cache:'no-store'});
        if(!response.ok) throw new Error('HTTP '+response.status);
        const data=await response.json();
        this.setState({snapshot:data,fault:''});
      }catch(err){this.setState({fault:'Cannot reach the monitoring API: '+err.message});}
    }
    async loadBench() {
      try {const r=await fetch('/api/benchmarks',{cache:'no-store'});if(r.ok)this.setState({benchmark:await r.json()});}catch(_){}
    }
    async action(path, body) {
      this.setState({busy:true,notice:''});
      try {
        const result=await fetch('/api/control/'+path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
        const data=await result.json();
        // FastAPI validation errors (422) carry a list: name the field and its limit.
        if(!result.ok)throw new Error(typeof data.detail==='string'?data.detail:
          Array.isArray(data.detail)?data.detail.map(d=>(d.loc||[]).slice(-1)[0]+': '+d.msg).join('; '):'Request rejected');
        this.setState({notice:data.message||data.note||'Requested successfully. Awaiting controller confirmation.'});
        this.refresh();
        return true;
      }catch(err){this.setState({notice:'Action failed: '+err.message});return false;}
      finally{this.setState({busy:false});}
    }
    async scenario(mode, traffic) {
      // Select the policy, give the controller one cycle to install it, then send traffic.
      if(!await this.action('mode',{mode}))return;
      await new Promise(resolve=>setTimeout(resolve,2500));
      await this.action('traffic',traffic);
    }
    render() {
      const snap=this.state.snapshot||{};
      const ctl=snap.controller||{};
      const backends=snap.backends||[];
      const healthy=backends.filter(b=>b.state==='HEALTHY').length;
      const active=backends.reduce((sum,b)=>sum+(b.in_system||0),0);
      const controls=snap.controls_enabled===true && ctl.alive && !snap.benchmark_active;
      const sample=snap.history||[];
      const job=snap.traffic_job||{};
      const isRunning=job.state==='running';
      const button=(label,fn,extra)=>h('button',{type:'button',disabled:!controls||this.state.busy,...extra,onClick:fn},label);
      return h('div',{className:'layout'},
        h('aside',{className:'sidebar'},
          h('div',{className:'brand'},h('span',{className:'brand-mark'},'◇'),h('span',null,'FLOW',h('b',null,'SCOPE'))),
          h('div',{className:'nav-label'},'WORKSPACE'),
          h('a',{href:'#overview',className:'navitem selected'},icon('◈'),' Overview'),
          h('a',{href:'#guide',className:'navitem'},icon('◎'),' Start here'),
          h('a',{href:'#network',className:'navitem'},icon('⊞'),' Data plane'),
          h('a',{href:'#latency',className:'navitem'},icon('◷'),' Latency'),
          h('a',{href:'#monitor',className:'navitem'},icon('▥'),' Backend health'),
          h('a',{href:'#controls',className:'navitem'},icon('⚙'),' Experiment lab'),
          h('a',{href:'#benchmarks',className:'navitem'},icon('▤'),' Benchmarks'),
          h('div',{className:'sidebar-bottom'},
            h('div',{className:'runtime-label'},'CURRENT RUNTIME'),
            h('div',{className:'runtime-text'},snap.runtime||'Connecting…'),
            h('div',{className:'sidebar-tag'},h('span',{className:'status-dot '+(ctl.alive?'on':'off')}),ctl.alive?'Controller connected':'Controller unavailable'))),
        h('main',{className:'main'},
          h('div',{className:'topbar'},h('div',{className:'breadcrumbs'},'NETWORK OPERATIONS',h('span',null,' / '),' LIVE DASHBOARD'),
            h('div',{className:'topbar-right'},h('span',{className:'auto-pill'},'● LIVE · 2s'),h('span',{className:'timestamp'},snap.timestamp?new Date(snap.timestamp*1000).toLocaleTimeString():'—'))),
          h('section',{id:'overview'},
            h('div',{className:'hero'},
              h('div',null,h('div',{className:'eyebrow'},'OPENFLOW 1.3  /  STATEFUL NAT'),h('h1',null,'SDN Control Center'),
                h('p',null,'Watch real traffic, create a controlled fault, and see how OVS responds. Start with the guided demo below.')),
              h('div',{className:'hero-badge'},badge(ctl.alive?'SYSTEM ONLINE':ctl.error?'CONTROLLER ERROR':'CONTROLLER OFFLINE',ctl.alive?'success':'danger'),
                h('div',{className:'hero-mode'},'Current policy',h('strong',null,(ctl.mode||'UNKNOWN').toUpperCase())))),
            this.state.fault?h('div',{className:'alert danger'},this.state.fault):null,
            h('div',{className:'metric-grid'},
              metric('TRAFFIC · LIVE', num(snap.total_rps,1)+' rps','Backend completions per second'),
              metric('BACKENDS HEALTHY',healthy+' / '+backends.length,'Independent /health probes'),
              metric('REQUESTS IN SYSTEM',int(active),'Queued + in service on all backends'),
              metric('CONTROL PLANE',ctl.alive?'Connected':ctl.error?'Error':'Offline',ctl.alive?'PID '+ctl.pid+' · '+num(ctl.age_s,1)+'s ago':ctl.error?ctl.error:'Serving last installed policy',ctl.alive?'accent':''))),
          h('section',{className:'panel quickstart',id:'guide'},
            sectionTitle('GET STARTED','See dynamic load balancing in four steps',badge(controls?'READY TO EXPERIMENT':'VIEW ONLY',controls?'success':'warning')),
            h('p',{className:'guide-intro'},'This is a live Mininet experiment. Your clients connect to one VIP, Open vSwitch chooses a backend for each new TCP connection, and the controller adjusts weights as conditions change.'),
            h('div',{className:'guide-grid'},
              h('div',{className:'guide-step'},h('span',{className:'step-index'},'01'),h('h3',null,'Learn the server sizes'),
                h('p',null,'b1, b2, b3 are a small, medium and large server (40, 80, 120 req/s). The controller is not told this. Send 150 req/s for 20 s and watch the weights move from 100:100:100 to about 50:100:150.'),
                button('Run steady traffic',()=>this.scenario('adaptive',{rate:150,duration:20}),{className:'primary-btn',disabled:!controls||this.state.busy||isRunning})),
              h('div',{className:'guide-step'},h('span',{className:'step-index'},'02'),h('h3',null,'Gray failure, adaptive'),
                h('p',null,'45 s at 150 req/s. From 15 s to 30 s b2 runs 100 ms slower (a third of its capacity) yet still passes health checks. Watch p95 rise, then fall as traffic moves off b2, and weights return when b2 recovers.'),
                button(isRunning?'Traffic in progress…':'Run with ADAPTIVE',()=>this.scenario('adaptive',{rate:150,duration:45,scenario:'gray-b2'}),{className:'primary-btn',disabled:!controls||this.state.busy||isRunning})),
              h('div',{className:'guide-step'},h('span',{className:'step-index'},'03'),h('h3',null,'Same failure, static weights'),
                h('p',null,'Repeat it with CAPACITY: fixed 50:100:150, what an operator would configure from the server sizes. They cannot react, so b2 stays overloaded until it recovers. The chart overlays both runs.'),
                button(isRunning?'Traffic in progress…':'Run with STATIC weights',()=>this.scenario('capacity',{rate:150,duration:45,scenario:'gray-b2'}),{className:'primary-btn',disabled:!controls||this.state.busy||isRunning})),
              h('div',{className:'guide-step'},h('span',{className:'step-index'},'04'),h('h3',null,'Compare and reset'),
                h('p',null,'Compare p95 and failures of the two runs in the latency chart, then reset: adaptive mode, no delays, no drains.'),
                button('Reset the lab',()=>this.action('reset',{}),{className:'secondary-btn',disabled:!controls||this.state.busy}),
                h('a',{href:'#latency',className:'guide-link'},'View latency chart ↓'))),
            h('div',{className:'guide-status'},
              h('span',{className:'status-dot '+(isRunning?'on':ctl.alive?'on':'off')}),
              isRunning?'Traffic generator is running. Charts update every 2 seconds.':job.state==='completed'?('Last test ('+(job.mode||'?').toUpperCase()+'): '+int(job.success)+' / '+int(job.offered)+' successful requests, p95 '+num(job.p95_ms,0)+' ms, p99 '+num(job.p99_ms,0)+' ms.'):job.state==='failed'?('Last test failed: '+(job.error||'Check the lab logs.')):'No traffic test running. Seeing 0 RPS is normal until you generate requests.'),
            this.state.notice?h('div',{className:'alert','role':'status'},this.state.notice):null,
            !controls?h('p',{className:'hint'},'Controls are unavailable if the server is read-only, the controller is offline, or a benchmark is active.'):null),
          h('div',{className:'two-col'},
            h('section',{className:'panel',id:'network'},sectionTitle('DATA PLANE','Traffic topology',badge('OVS SELECT + CT','muted')),h(NetworkTopology,{backends:backends,vip:snap.vip})),
            h('section',{className:'panel'},sectionTitle('REQUEST FLOW','Measured traffic rate',h('span',{className:'mini-label'},'Last '+sample.length+' samples')),
              h(Sparkline,{points:sample.map(s=>s.rps),minimum:10}),
              h('div',{className:'chart-bottom'},'Rates are calculated from consecutive backend completion counters, not generated estimates.'))),
          h('section',{className:'panel',id:'latency'},sectionTitle('WHAT CLIENTS SEE','End-to-end latency per second (p95)',badge('CLIENT-MEASURED','muted')),
            h(LatencyChart,{runs:snap.traffic_runs}),
            h('div',{className:'chart-bottom'},'Measured by the traffic generator in h1 for every request, including queueing at the backend. Red dots: seconds with failed or rejected requests.')),
          h('section',{className:'panel',id:'monitor'},
            sectionTitle('LIVE TELEMETRY','Backend monitoring',badge('DIRECT PROBES','muted')),
            h('div',{className:'backend-grid'},backends.map((b,i)=>h('div',{className:'backend-card',key:b.name},
              h('div',{className:'backend-card-header'},h('div',{className:'server-title'},h('span',{className:'server-token',style:{background:swatches[i]}},b.name.slice(1)),h('div',null,h('strong',null,'Backend '+b.name.slice(1)),h('small',null,b.ip))),badge(b.state,(b.state==='HEALTHY'?'success':b.state==='DOWN'?'danger':'warning'))),
              h('div',{className:'server-stat-row'},h('span',null,'Load per worker'),h('strong',null,b.load==null?'—':num(100*b.load,0)+'%')),
              progress(100*(b.load||0),b.load>1?'#ff8496':swatches[i]),
              h('div',{className:'server-grid'},
                h('div',null,h('small',null,'PROBE RTT'),h('strong',null,num(b.probe_ms,2)+' ms')),
                h('div',null,h('small',null,'RESPONSE TIME EMA'),h('strong',null,num(b.response_ema_ms,1)+' ms')),
                h('div',null,h('small',null,'SERVER SIZE'),h('strong',null,int(b.workers)+' workers')),
                h('div',null,h('small',null,'INSTALLED WEIGHT'),h('strong',null,int(b.weight)))),
              h('div',{className:'backend-bottom'},h('span',null,h('span',{className:'status-dot '+(b.probe_ok?'on':'off')}),b.probe_ok?'Probe healthy':'Probe unreachable'),
                h('span',null,num(b.rps,1)+' rps'))))),
            h('div',{className:'monitor-footer'},'Load per worker = average requests in the system (queued + in service) per worker; above 100 % a queue is building. Response time includes queueing. Neither is end-to-end P95.')),
          h('div',{className:'two-col compact'},
            h('section',{className:'panel',id:'distribution'},sectionTitle('COMPLETED REQUESTS','Backend traffic distribution',badge(snap.window_requests+' requests','muted')),
              h(TrafficSplit,{backends:backends}),h('p',{className:'footnote'},'Shares count requests completed during the last 2-second sample. They are not exact connection counts. Weights affect new TCP connections, not existing ones.')),
            h('section',{className:'panel'},sectionTitle('CONTROL PLANE','Reconciliation & policy'),
              h('div',{className:'control-info'},
                h('div',null,h('small',null,'POLICY'),h('strong',null,(ctl.mode||'—').toUpperCase())),
                h('div',null,h('small',null,'INSTALLED / DESIRED'),h('strong',null,ctl.synced?'SYNCHRONIZED':'DIFFERENT'))),
              h('div',{className:'weight-table'},backends.map((b,i)=>h('div',{className:'weight-row',key:b.name},
                h('span',{style:{color:swatches[i]}},b.name),
                progress(b.weight/3,swatches[i]),h('strong',null,int(b.weight)+' / '+int(b.desired_weight)),
                h('small',null,'switch '+(b.observed_share_pct==null?'—':num(b.observed_share_pct,0)+'%'))))),
              h('div',{className:'footnote'},'Installed / desired SELECT bucket weights (sum 300), and the share of new connections the switch actually sent to each backend, from OpenFlow group bucket counters.'))),
          h('section',{className:'panel explainer',id:'glossary'},
            sectionTitle('QUICK REFERENCE','How to read these numbers'),
            h('div',{className:'glossary-grid'},
              h('div',null,h('strong',null,'Traffic rate (RPS)'),h('p',null,'Completed backend HTTP requests per second. Zero is expected when no client is sending traffic.')),
              h('div',null,h('strong',null,'Weight (installed / desired)'),h('p',null,'How OVS is configured to choose backends for NEW TCP connections. 100/100 means installed and requested settings match.')),
              h('div',null,h('strong',null,'Load per worker'),h('p',null,'What the adaptive controller equalizes. Equal load everywhere means each backend gets traffic in proportion to its capacity.')),
              h('div',null,h('strong',null,'Health and probe RTT'),h('p',null,'Whether /health responds, and its network round-trip time. A slow /work request can still have a healthy probe.')),
              h('div',null,h('strong',null,'P95 latency'),h('p',null,'95% of requests finished within this time, measured by the client. The latency chart shows it per second; the benchmark table per run.')))),
          h('section',{className:'panel',id:'controls'},
            sectionTitle('EXPERIMENT WORKBENCH','Advanced experiment controls',badge(controls?'CONTROLS ENABLED':'READ-ONLY',controls?'success':'warning')),
            !snap.controls_enabled?h('div',{className:'hint'},'Start the monitoring server with --allow-controls to enable bounded lab actions.'):null,
            snap.benchmark_active?h('div',{className:'alert warning'},'A benchmark is running. Dashboard controls are locked to preserve experimental results.'):null,
            h('p',{className:'hint'},'The guided demo above is the easiest starting point. Open these controls for policy comparisons, fault injection, or backend maintenance.'),
            h('div',{className:'control-toolbar'},
              h('div',{className:'active-profiles'},'Current: ',
                h('strong',null,(ctl.mode||'unknown').toUpperCase()+' mode'),
                backends.filter(b=>b.draining||(b.workload&&b.workload.delay_ms)).map(b=>h('span',{className:'profile-chip',key:b.name},b.name+(b.draining?' draining':'')+(b.workload&&b.workload.delay_ms?' · '+b.workload.delay_ms+'ms delay':'')))),
              h('button',{type:'button',className:'toggle-btn',onClick:()=>this.setState({showAdvanced:!this.state.showAdvanced}),'aria-expanded':this.state.showAdvanced},this.state.showAdvanced?'Hide advanced controls ▲':'Show advanced controls ▼')),
            this.state.showAdvanced?h('div',{className:'controls-grid'},
              h('div',{className:'control-block'},h('h3',null,'Selection policy'),h('p',null,'ADAPTIVE equalizes measured load per worker; EQUAL uses 100:100:100; CAPACITY uses fixed weights proportional to the server sizes (50:100:150), as an operator would configure. Existing connections remain pinned by conntrack.'),
                h('div',{className:'mode-buttons'},['adaptive','equal','capacity'].map(mode=>button(mode.toUpperCase(),()=>this.action('mode',{mode}),{key:mode,className:'mode-btn '+(ctl.mode===mode?'active':'')})))),
              h('div',{className:'control-block'},h('h3',null,'Backend maintenance'),h('p',null,'Drain = no NEW connections to that backend; Crash = stop the worker (existing connections may fail); Restart = start it and wait for recovery.'),
                h('div',{className:'drain-list'},backends.map(b=>h('div',{className:'drain-row',key:b.name},
                  h('span',null,b.name.toUpperCase(),b.draining?' · draining':''),
                  h('div',{className:'maintenance-actions'},
                    button(b.draining?'Undrain':'Drain',()=>this.action('drain',{backend:b.name,enabled:!b.draining}),{className:'tiny-btn'}),
                    button('Crash',()=>{if(window.confirm('Stop '+b.name+' HTTP server? Active sessions to it may fail.'))this.action('crash',{backend:b.name});},{className:'tiny-btn danger-btn'}),
                    button('Restart',()=>this.action('restart',{backend:b.name}),{className:'tiny-btn'})))))),
              h('div',{className:'control-block'},h('h3',null,'Inject backend load'),h('p',null,'Delay (ms) is added to every /work request while it holds a worker, so it cuts that backend\'s capacity. It stays active until changed or reset.'),
                h('div',{className:'field-row'},h('label',null,'BACKEND',h('select',{value:this.state.editingBackend,onChange:e=>this.setState({editingBackend:e.target.value})},names.map(n=>h('option',{value:n,key:n},n)))),
                  h('label',null,'DELAY (MS)',h('input',{type:'number',min:0,max:350,value:this.state.delay,onChange:e=>this.setState({delay:Number(e.target.value)})}))),
                button('Apply workload',()=>this.action('workload',{backend:this.state.editingBackend,delay_ms:this.state.delay}),{className:'primary-btn'})),
              h('div',{className:'control-block'},h('h3',null,'Generate sample traffic'),h('p',null,'Generate new TCP connections from h1 through the VIP. A run fills the live traffic and backend-distribution panels.'),
                h('div',{className:'field-row'},h('label',null,'REQUESTS/SEC',h('input',{type:'number',min:1,max:300,value:this.state.rate,onChange:e=>this.setState({rate:Number(e.target.value)})})),
                  h('label',null,'SECONDS',h('input',{type:'number',min:3,max:60,value:this.state.duration,onChange:e=>this.setState({duration:Number(e.target.value)})}))),
                button(isRunning?'Traffic test running…':'Start traffic test',()=>this.action('traffic',{rate:this.state.rate,duration:this.state.duration}),{className:'primary-btn',disabled:!controls||this.state.busy||isRunning}),
                job.state?h('div',{className:'job-status'},'Last traffic test: '+job.state+(job.success!=null?' · '+job.success+'/'+job.offered+' succeeded':'')) : null)) : null,
            h('div',{className:'control-footer'},button('Reset workloads + drains + adaptive mode',()=>this.action('reset',{}),{className:'reset-btn'}),
              h('span',null,'Reset clears injected delays and drains, and selects adaptive policy. It does not restart a crashed worker.')),
            this.state.notice?h('div',{className:'alert'},this.state.notice):null),
          h('div',{className:'two-col lower'},
            h('section',{className:'panel',id:'benchmarks'},sectionTitle('SAVED RESULTS','Benchmark comparison (not live)'),h(BenchmarkTable,{data:this.state.benchmark||{}})),
            h('section',{className:'panel'},sectionTitle('RECENT ACTIVITY','Controller event log'),
              h('div',{className:'event-list'},(snap.events||[]).slice(-10).reverse().map((e,i)=>h('details',{className:'event-details',key:i},
                h('summary',null,friendlyEvent(e.message)),h('pre',null,e.message)))) ,
              h('p',{className:'footnote'},'Newest events appear first. Expand an entry to inspect the original controller log (no timestamps in source).'))),
          h('div',{className:'footer'},h('span',null,'FLOWSCOPE  /  INTERACTIVE LAB'),h('span',null,'Live OVS + backend telemetry · loopback-only dashboard'))));
    }
  }
  ReactDOM.render(h(App),document.getElementById('root'));
})();
