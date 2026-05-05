/* ═══════════════════════════════════════════════════════════
   Web App Porto de Lisboa
   ═══════════════════════════════════════════════════════════
   Lê o JSON gerado pelo scraper que corre no GitHub Actions.

   ► IMPORTANTE: Edita a constante DATA_URL abaixo, substituindo
     SEU-USER e SEU-REPO pelos teus valores reais.
*/

class WindowComponent {

    /* ─── URL do JSON gerado pelo scraper no GitHub ─── */
    DATA_URL = 'https://raw.githubusercontent.com/SEU-USER/SEU-REPO/main/data/lisbon_port.json';
    REFRESH_MS = 5 * 60 * 1000;   // a web-app re-lê o ficheiro a cada 5 min

    /* ─── Estado ─── */
    map;
    cruiseSrc; cruiseLyr;
    hazardSrc; hazardLyr;
    termSrc;   termLyr;
    popup;

    TODAY;
    DAYS = [-2, -1, 0, 1, 2];
    HARD_MIN = -2;
    HARD_MAX = 2;
    SHIPS = [];
    HAZARD_DATA = [];

    modeCruise = true;
    modeHazard = false;
    activeTypes;
    sliderMin = 0;
    sliderMax = 4;
    selGroupKey = null;
    shipFeatureMap = {};
    hazardFeatures = [];

    clockInterval;
    refreshInterval;
    initTimeout;

    /* ─── Constantes geográficas ─── */
    CRUISE_TERMS = {
        'Santa Apolónia':     [-9.121528, 38.713556],
        'Jardim do Tabaco':   [-9.1318,   38.7072  ],
        'Rocha Conde Óbidos': [-9.1678,   38.7002  ],
        'Alcântara':          [-9.1792,   38.6985  ],
    };
    HAZARD_TERMS = {
        'Terminal Multiusos do Poço do Bispo': [-9.101361, 38.738528],
    };

    TYPE_COLOR = {
        arrival:    '#2a6060',
        departure:  '#b5614a',
        transit:    '#b8960a',
        turnaround: '#4a8a5a',
    };
    TYPE_LBL = { arrival:'Chegada', departure:'Partida', transit:'Trânsito', turnaround:'Turnaround', hazard:'IMDG' };
    TAG_CLS  = { arrival:'stag-arrival', departure:'stag-departure', transit:'stag-transit', turnaround:'stag-turnaround', hazard:'stag-hazard' };
    CARD_CLS = { arrival:'t-arrival',    departure:'t-departure',    transit:'t-transit',    turnaround:'t-turnaround',    hazard:'t-hazard' };

    JITTER = [
        [0, 0], [0.0006, 0.0003], [-0.0006, 0.0003],
        [0, 0.0007], [0.0007, -0.0003], [-0.0007, -0.0003],
    ];

    /* ═══════════════════════════════════════════════════
       LIFECYCLE
       ═══════════════════════════════════════════════════ */
    ngOnInit() {
        console.clear();
        console.log("🚢 INIT Porto de Lisboa");

        this.TODAY = new Date(); this.TODAY.setHours(0, 0, 0, 0);
        this.activeTypes = new Set(['arrival', 'departure', 'transit', 'turnaround', 'hazard']);

        this.initTimeout = setTimeout(() => this.init(), 300);
    }

    ngOnDestroy() {
        if (this.clockInterval)   clearInterval(this.clockInterval);
        if (this.refreshInterval) clearInterval(this.refreshInterval);
        if (this.initTimeout)     clearTimeout(this.initTimeout);
    }

    /* ═══════════════════════════════════════════════════
       INIT
       ═══════════════════════════════════════════════════ */
    init() {
        this.popup = document.getElementById('ship-popup');

        this.initMap();
        this.bindEvents();
        this.buildTicks();
        this.updateSliderUI();
        this.startClock();
        this.syncFilterVisibility();
        this.rebuildTerminals();

        // 1ª carga e auto-refresh
        this.loadData();
        this.refreshInterval = setInterval(() => this.loadData(), this.REFRESH_MS);
    }

    /* ═══════════════════════════════════════════════════
       LOAD DATA — uma chamada simples ao JSON do GitHub
       ═══════════════════════════════════════════════════ */
    async loadData() {
        const now = new Date();
        console.log('🔄 loadData @', now.toLocaleTimeString('pt-PT'));

        // Recalcular TODAY (em caso de meia-noite)
        const newToday = new Date(); newToday.setHours(0,0,0,0);
        const dayChanged = newToday.getTime() !== this.TODAY.getTime();
        this.TODAY = newToday;
        if (dayChanged) { this.buildTicks(); this.updateSliderUI(); }

        try {
            // Cache-buster para garantir que apanhamos a versão mais recente
            const url = this.DATA_URL + '?_=' + Date.now();
            const r = await fetch(url, { mode: 'cors', cache: 'no-store' });
            if (!r.ok) throw new Error('HTTP ' + r.status);

            const data = await r.json();
            this.assignDataset(data);
            this.markSource(data.fetched_at);
            this.hideLoading();
            this.updateView();

        } catch (e) {
            console.error('Falha ao carregar dados:', e);
            this.showError(
                `Não foi possível carregar dados.<br><br>` +
                `<span style="font-size:0.55rem;text-transform:none;letter-spacing:0">${e.message}</span><br><br>` +
                `Verifica se o URL no JS está correcto e se o<br>` +
                `repositório do scraper é público.`
            );
        }
    }

    /* ─── Conversão JSON → estado interno ─── */
    assignDataset(data) {
        const ships   = (data?.ships   || []).map(r => this.normaliseRecord(r)).filter(Boolean);
        const hazards = (data?.hazards || []).map(r => this.normaliseRecord(r)).filter(Boolean);
        this.SHIPS = ships;
        this.HAZARD_DATA = hazards;
        console.log(`  → ${ships.length} cruzeiros · ${hazards.length} matérias perigosas`);
    }

    normaliseRecord(r) {
        if (!r || !r.name || !r.date) return null;
        const date = new Date(r.date);
        if (isNaN(date.getTime())) return null;

        const offset = this.diffDays(date, this.TODAY);
        if (offset < this.HARD_MIN || offset > this.HARD_MAX) return null;   // descartar fora ±2d

        const isHazard = !!r.is_hazard || r.type === 'hazard';
        const term = r.terminal || (isHazard ? 'Terminal Multiusos do Poço do Bispo' : 'Alcântara');
        const coords = isHazard
            ? this.HAZARD_TERMS['Terminal Multiusos do Poço do Bispo']
            : (this.CRUISE_TERMS[term] || this.CRUISE_TERMS['Alcântara']);

        return {
            name:     r.name,
            line:     r.line || '',
            type:     isHazard ? 'hazard' : (r.type || 'arrival'),
            terminal: term,
            from:     r.from || '',
            to:       r.to || '',
            date,
            hour:     r.hour || date.toLocaleTimeString('pt-PT', { hour:'2-digit', minute:'2-digit' }),
            pax:      Number(r.pax) || 0,
            cargo:    r.cargo || '',
            offset,
            coords,
            kind:     isHazard ? 'hazard' : 'cruise',
        };
    }

    diffDays(a, b) {
        const aMid = new Date(a); aMid.setHours(0,0,0,0);
        const bMid = new Date(b); bMid.setHours(0,0,0,0);
        return Math.round((aMid - bMid) / (1000 * 60 * 60 * 24));
    }

    /* ─── Mensagens na barra de status / overlay ─── */
    markSource(fetchedAt) {
        const el = document.getElementById('data-source');
        if (!el) return;
        let when = '—';
        if (fetchedAt) {
            try {
                const d = new Date(fetchedAt);
                when = d.toLocaleTimeString('pt-PT', { hour:'2-digit', minute:'2-digit' })
                     + ' · ' + d.toLocaleDateString('pt-PT');
            } catch {}
        }
        el.textContent = `Fonte: portodelisboa.pt · última recolha ${when}`;
    }

    hideLoading() {
        const ov = document.getElementById('loading-overlay');
        if (ov) { ov.classList.add('hidden'); ov.classList.remove('error'); }
    }

    showError(html) {
        const ov  = document.getElementById('loading-overlay');
        const txt = document.getElementById('loading-text');
        if (ov && txt) {
            ov.classList.remove('hidden');
            ov.classList.add('error');
            txt.innerHTML = html;
        }
    }

    /* ═══════════════════════════════════════════════════
       HELPERS DE DATA
       ═══════════════════════════════════════════════════ */
    dayOff(n)    { const d = new Date(this.TODAY); d.setDate(d.getDate() + n); return d; }
    fmtShort(d)  { return d.toLocaleDateString('pt-PT', {day:'2-digit', month:'short'}); }
    fmtFull(d)   { return d.toLocaleDateString('pt-PT', {day:'2-digit', month:'2-digit', year:'numeric'}); }
    DAY_LBL(n)   { return ({'-2':'Anteontem','-1':'Ontem','0':'Hoje','1':'Amanhã','2':'Depois de amanhã'})[n] || '—'; }

    /* ═══════════════════════════════════════════════════
       ÍCONES (canvas)
       ═══════════════════════════════════════════════════ */
    roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
    }

    makeShipIcon(dots, selected) {
        const dotR = 7, pad = 6, gap = 5;
        const n  = Math.max(dots.length, 1);
        const W  = n * dotR * 2 + (n - 1) * gap + pad * 2;
        const H  = dotR * 2 + pad * 2;
        const sc = selected ? 1.15 : 1;
        const cw = Math.round(W * sc * 1.3);
        const ch = Math.round(H * sc * 1.3);
        const c  = document.createElement('canvas');
        c.width = cw; c.height = ch;
        const ctx = c.getContext('2d');
        ctx.scale(cw / W, ch / H);

        ctx.shadowColor = 'rgba(0,0,0,0.22)'; ctx.shadowBlur = selected ? 6 : 4; ctx.shadowOffsetY = 1;
        ctx.fillStyle = '#FAFAFA';
        this.roundRect(ctx, 0, 0, W, H, H / 2);
        ctx.fill();
        ctx.shadowColor = 'transparent';

        if (selected) {
            ctx.strokeStyle = '#5E718D'; ctx.lineWidth = 2;
            this.roundRect(ctx, 1, 1, W - 2, H - 2, H / 2);
            ctx.stroke();
        }

        const startX = pad + dotR;
        dots.forEach((d, i) => {
            const cx = startX + i * (dotR * 2 + gap);
            const cy = H / 2;
            ctx.beginPath(); ctx.arc(cx, cy, dotR, 0, Math.PI * 2);
            ctx.fillStyle = this.TYPE_COLOR[d.type] || '#888';
            ctx.fill();
            ctx.strokeStyle = 'rgba(255,255,255,0.7)'; ctx.lineWidth = 1.2; ctx.stroke();
        });

        return c;
    }

    makeHazardShipIcon(selected) {
        const triH = 13, triW = 14, pad = 6;
        const W  = triW + pad * 2;
        const H  = triH + pad * 2;
        const sc = selected ? 1.15 : 1;
        const cw = Math.round(W * sc * 1.3);
        const ch = Math.round(H * sc * 1.3);
        const c  = document.createElement('canvas');
        c.width = cw; c.height = ch;
        const ctx = c.getContext('2d');
        ctx.scale(cw / W, ch / H);

        ctx.shadowColor = 'rgba(0,0,0,0.22)'; ctx.shadowBlur = selected ? 6 : 4; ctx.shadowOffsetY = 1;
        ctx.fillStyle = '#FAFAFA';
        this.roundRect(ctx, 0, 0, W, H, 5);
        ctx.fill();
        ctx.shadowColor = 'transparent';

        if (selected) {
            ctx.strokeStyle = '#cc1a1a'; ctx.lineWidth = 2;
            this.roundRect(ctx, 1, 1, W - 2, H - 2, 5);
            ctx.stroke();
        }

        const cx = W / 2, cy = H / 2;
        ctx.beginPath();
        ctx.moveTo(cx,            cy - triH / 2);
        ctx.lineTo(cx + triW / 2, cy + triH / 2);
        ctx.lineTo(cx - triW / 2, cy + triH / 2);
        ctx.closePath();
        ctx.fillStyle = '#cc1a1a'; ctx.fill();
        ctx.strokeStyle = 'rgba(255,255,255,0.7)'; ctx.lineWidth = 1.2; ctx.stroke();

        return c;
    }

    makeTermIcon() {
        const S = 14, PAD = 3;
        const cw = Math.round((S + PAD * 2) * 1.3);
        const ch = Math.round((S + PAD * 2) * 1.3);
        const c  = document.createElement('canvas');
        c.width = cw; c.height = ch;
        const ctx = c.getContext('2d');
        ctx.scale(cw / (S + PAD * 2), ch / (S + PAD * 2));

        ctx.shadowColor = 'rgba(0,0,0,0.25)'; ctx.shadowBlur = 3; ctx.shadowOffsetY = 1;
        ctx.fillStyle = '#2D3643';
        ctx.fillRect(PAD, PAD, S, S);
        ctx.shadowColor = 'transparent';
        return c;
    }

    toOlIcon(canvas, anchorY = 0.5) {
        return new ol_style.Icon({
            img: canvas,
            imgSize: [canvas.width, canvas.height],
            anchor: [0.5, anchorY],
            anchorXUnits: 'fraction',
            anchorYUnits: 'fraction',
        });
    }

    /* ═══════════════════════════════════════════════════
       MAPA
       ═══════════════════════════════════════════════════ */
    initMap() {

        this.cruiseSrc = new ol_source.Vector();
        this.cruiseLyr = new ol_layer.Vector({ source: this.cruiseSrc, zIndex: 5 });
        this.hazardSrc = new ol_source.Vector();
        this.hazardLyr = new ol_layer.Vector({ source: this.hazardSrc, zIndex: 4 });
        this.termSrc   = new ol_source.Vector();
        this.termLyr   = new ol_layer.Vector({ source: this.termSrc,   zIndex: 3 });

        const baseLayer = new ol_layer.Tile({
            source: new ol_source.XYZ({
                url: 'https://{a-c}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
                maxZoom: 19
            })
        });

        this.map = new ol.Map({
            target: 'map',
            layers: [baseLayer, this.termLyr, this.hazardLyr, this.cruiseLyr],
            view: new ol.View({
                center: ol_proj.fromLonLat([-9.148, 38.706]),
                zoom: 13,
                minZoom: 9,
                maxZoom: 18
            }),
            controls: [ new ol_control.Zoom() ]
        });

        // Pointer move (popup)
        this.map.on('pointermove', (evt) => {
            const px = this.map.getEventPixel(evt.originalEvent);
            const hit = this.map.forEachFeatureAtPixel(px, f => f);

            if (hit && hit.get('groupData')) {
                const g = hit.get('groupData');
                const first = g.ships[0];
                document.getElementById('p-name').textContent  = g.name;
                document.getElementById('p-line').textContent  = first.line;
                const tipos = g.ships.map(s => ({arrival:'Chegada',departure:'Partida',transit:'Trânsito',turnaround:'Turnaround'})[s.type]).join(' + ');
                document.getElementById('p-type').textContent  = tipos;
                document.getElementById('p-date').textContent  = g.ships.map(s => this.fmtFull(s.date) + ' ' + s.hour).join(' / ');
                document.getElementById('p-pax-lbl').textContent = 'Passageiros';
                document.getElementById('p-pax').textContent   = (first.pax || 0).toLocaleString('pt-PT');
                document.getElementById('p-from').textContent  = first.from;
                document.getElementById('p-to').textContent    = g.ships[g.ships.length - 1].to;
                this.showPopup(px);
            } else if (hit && hit.get('hazardData')) {
                const s = hit.get('hazardData');
                document.getElementById('p-name').textContent  = s.name;
                document.getElementById('p-line').textContent  = s.line;
                document.getElementById('p-type').textContent  = 'Carga IMDG';
                document.getElementById('p-date').textContent  = this.fmtFull(s.date) + ' · ' + s.hour;
                document.getElementById('p-pax-lbl').textContent = 'Carga';
                document.getElementById('p-pax').textContent   = s.cargo;
                document.getElementById('p-from').textContent  = s.from;
                document.getElementById('p-to').textContent    = s.to;
                this.showPopup(px);
            } else if (!hit?.get('isTerminal')) {
                this.popup.classList.remove('vis');
                this.map.getTargetElement().style.cursor = '';
            }
        });

        // Click (selecção de navio)
        this.map.on('click', (evt) => {
            const px = this.map.getEventPixel(evt.originalEvent);
            const hit = this.map.forEachFeatureAtPixel(px, f => f);
            if (hit && hit.get('groupKey')) {
                const k = hit.get('groupKey');
                this.selGroupKey = this.selGroupKey === k ? null : k;
                this.renderCruiseShips(this.currentFiltered().cruise);

                document.querySelectorAll('.lpb-container .ship-card').forEach(c => {
                    c.classList.toggle('active', c.dataset.groupKey === this.selGroupKey);
                });

                if (this.selGroupKey) {
                    const f = this.shipFeatureMap[this.selGroupKey];
                    if (f) this.map.getView().animate({
                        center: f.getGeometry().getCoordinates(),
                        zoom: 14,
                        duration: 500
                    });
                    document.querySelector(`.lpb-container .ship-card[data-group-key="${this.selGroupKey}"]`)?.scrollIntoView({block:'nearest', behavior:'smooth'});
                }
            }
        });
    }

    showPopup(px) {
        const rect = this.map.getTargetElement().getBoundingClientRect();
        let l = px[0] + 16, t = px[1] - 60;
        if (l + 240 > rect.width) l = px[0] - 246;
        if (t < 0) t = 4;
        this.popup.style.left = l + 'px';
        this.popup.style.top  = t + 'px';
        this.popup.classList.add('vis');
        this.map.getTargetElement().style.cursor = 'pointer';
    }

    /* ═══════════════════════════════════════════════════
       TERMINAIS
       ═══════════════════════════════════════════════════ */
    rebuildTerminals() {
        if (!this.termSrc) return;
        this.termSrc.clear();
        const format = new ol_format.GeoJSON();
        const opts = { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' };

        if (this.modeCruise) {
            Object.entries(this.CRUISE_TERMS).forEach(([name, ll]) => {
                const f = format.readFeature({
                    type: 'Feature',
                    geometry: { type: 'Point', coordinates: ll },
                    properties: { isTerminal: true, termName: name }
                }, opts);
                f.setStyle(new ol_style.Style({ image: this.toOlIcon(this.makeTermIcon()) }));
                this.termSrc.addFeature(f);
            });
        }

        if (this.modeHazard) {
            Object.entries(this.HAZARD_TERMS).forEach(([name, ll]) => {
                const f = format.readFeature({
                    type: 'Feature',
                    geometry: { type: 'Point', coordinates: ll },
                    properties: { isTerminal: true, termName: name }
                }, opts);
                f.setStyle(new ol_style.Style({ image: this.toOlIcon(this.makeTermIcon()) }));
                this.termSrc.addFeature(f);
            });
        }
    }

    /* ═══════════════════════════════════════════════════
       AGRUPAR + RENDERIZAR NAVIOS
       ═══════════════════════════════════════════════════ */
    groupCruiseShips(list) {
        const groups = {};
        list.forEach(s => {
            const key = s.name + '|' + s.terminal;
            if (!groups[key]) groups[key] = { key, name: s.name, terminal: s.terminal, coords: s.coords, ships: [] };
            groups[key].ships.push(s);
        });
        return Object.values(groups);
    }

    renderCruiseShips(list) {
        if (!this.cruiseSrc) return;
        this.cruiseSrc.getFeatures().filter(f => !f.get('isTerminal')).forEach(f => this.cruiseSrc.removeFeature(f));
        this.shipFeatureMap = {};
        if (!this.modeCruise) return;

        const groups = this.groupCruiseShips(list);
        const termCount = {};
        const format = new ol_format.GeoJSON();
        const opts = { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' };

        groups.forEach((g) => {
            termCount[g.terminal] = termCount[g.terminal] || 0;
            const ji = termCount[g.terminal]++;
            const [dx, dy] = this.JITTER[ji % this.JITTER.length];

            const isSel = (this.selGroupKey === g.key);
            const dots  = g.ships.map(s => ({ type: s.type }));
            const icon  = this.toOlIcon(this.makeShipIcon(dots, isSel));

            const f = format.readFeature({
                type: 'Feature',
                geometry: { type: 'Point', coordinates: [g.coords[0] + dx, g.coords[1] + dy] }
            }, opts);
            f.set('groupKey', g.key);
            f.set('groupData', g);
            f.set('kind', 'cruise');
            f.setStyle(new ol_style.Style({ image: icon }));
            this.cruiseSrc.addFeature(f);
            this.shipFeatureMap[g.key] = f;
        });
    }

    renderHazardShips(list) {
        if (!this.hazardSrc) return;
        this.hazardSrc.getFeatures().filter(f => !f.get('isTerminal')).forEach(f => this.hazardSrc.removeFeature(f));
        this.hazardFeatures = [];
        if (!this.modeHazard) return;

        const format = new ol_format.GeoJSON();
        const opts = { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' };

        list.forEach((s, i) => {
            const jx = (i - 2) * 0.0018;
            const jy = (i % 2 === 0 ? 0.001 : -0.001);

            const f = format.readFeature({
                type: 'Feature',
                geometry: { type: 'Point', coordinates: [s.coords[0] + jx, s.coords[1] + jy] }
            }, opts);
            f.set('hazardData', s);
            f.set('kind', 'hazard');
            f.setStyle(new ol_style.Style({ image: this.toOlIcon(this.makeHazardShipIcon(false)) }));
            this.hazardSrc.addFeature(f);
            this.hazardFeatures.push(f);
        });
    }

    /* ═══════════════════════════════════════════════════
       SLIDER
       ═══════════════════════════════════════════════════ */
    buildTicks() {
        const el = document.getElementById('day-ticks');
        if (!el) return;
        el.innerHTML = '';
        this.DAYS.forEach(d => {
            const div = document.createElement('div');
            div.className = 'day-tick' + (d === 0 ? ' today' : '');
            div.innerHTML = (d === 0)
                ? `<strong>Hoje</strong><br>${this.fmtShort(this.dayOff(d))}`
                : `${d > 0 ? '+' : ''}${d}d<br>${this.fmtShort(this.dayOff(d))}`;
            el.appendChild(div);
        });
    }

    updateSliderUI() {
        const rsEl = document.getElementById('rs');
        const reEl = document.getElementById('re');
        if (!rsEl || !reEl) return;

        const s = +rsEl.value, e = +reEl.value;
        document.getElementById('sl-fill').style.left  = (s / 4 * 100) + '%';
        document.getElementById('sl-fill').style.width = ((e - s) / 4 * 100) + '%';
        document.getElementById('chip-s').textContent  = this.fmtShort(this.dayOff(this.DAYS[s]));
        document.getElementById('chip-e').textContent  = this.fmtShort(this.dayOff(this.DAYS[e]));
        this.sliderMin = this.DAYS[s];
        this.sliderMax = this.DAYS[e];
    }

    /* ═══════════════════════════════════════════════════
       LIGAÇÃO DE EVENTOS
       ═══════════════════════════════════════════════════ */
    bindEvents() {
        const tabCruise = document.getElementById('tab-cruise');
        const tabHazard = document.getElementById('tab-hazard');
        if (tabCruise) tabCruise.onclick = () => this.toggleMode('cruise');
        if (tabHazard) tabHazard.onclick = () => this.toggleMode('hazard');

        document.querySelectorAll('.lpb-container .type-btn').forEach(btn => {
            btn.onclick = () => this.toggleType(btn.dataset.type);
        });

        const rsEl = document.getElementById('rs');
        const reEl = document.getElementById('re');
        if (rsEl && reEl) {
            rsEl.addEventListener('input', () => {
                if (+rsEl.value > +reEl.value) reEl.value = rsEl.value;
                this.updateSliderUI(); this.updateView();
            });
            reEl.addEventListener('input', () => {
                if (+reEl.value < +rsEl.value) rsEl.value = reEl.value;
                this.updateSliderUI(); this.updateView();
            });
        }
    }

    /* ═══════════════════════════════════════════════════
       MODOS / FILTROS
       ═══════════════════════════════════════════════════ */
    toggleMode(m) {
        if (m === 'cruise') this.modeCruise = !this.modeCruise;
        else                this.modeHazard = !this.modeHazard;

        if (!this.modeCruise && !this.modeHazard) {
            if (m === 'cruise') this.modeCruise = true;
            else                this.modeHazard = true;
        }

        document.getElementById('tab-cruise').className = 'tab-btn' + (this.modeCruise ? ' on-cruise' : '');
        document.getElementById('tab-hazard').className = 'tab-btn' + (this.modeHazard ? ' on-hazard' : '');
        this.syncFilterVisibility();
        this.rebuildTerminals();
        this.updateView();
    }

    syncFilterVisibility() {
        ['arrival','departure','transit','turnaround'].forEach(t => {
            const el = document.querySelector(`.lpb-container .type-btn[data-type="${t}"]`);
            if (el) el.style.display = this.modeCruise ? '' : 'none';
        });
        const hz = document.querySelector('.lpb-container .type-btn[data-type="hazard"]');
        if (hz) hz.style.display = this.modeHazard ? '' : 'none';
    }

    toggleType(t) {
        const btn = document.querySelector(`.lpb-container .type-btn[data-type="${t}"]`);
        const visActive = [...this.activeTypes].filter(x => {
            const el = document.querySelector(`.lpb-container .type-btn[data-type="${x}"]`);
            return el && el.style.display !== 'none';
        });

        if (this.activeTypes.has(t)) {
            if (visActive.length === 1) return;
            this.activeTypes.delete(t);
            btn.classList.remove('on');
        } else {
            this.activeTypes.add(t);
            btn.classList.add('on');
        }
        this.updateView();
    }

    /**
     * Filtragem com 2 níveis:
     *   1) Limite duro ±2 dias.
     *   2) Janela do slider.
     */
    currentFiltered() {
        const inHard   = s => s.offset >= this.HARD_MIN   && s.offset <= this.HARD_MAX;
        const inSlider = s => s.offset >= this.sliderMin  && s.offset <= this.sliderMax;

        const cruise = this.modeCruise
            ? this.SHIPS.filter(s => inHard(s) && inSlider(s) && this.activeTypes.has(s.type))
            : [];
        const hazard = this.modeHazard
            ? this.HAZARD_DATA.filter(s => inHard(s) && inSlider(s) && this.activeTypes.has('hazard'))
            : [];
        return { cruise, hazard };
    }

    /* ═══════════════════════════════════════════════════
       LISTA LATERAL
       ═══════════════════════════════════════════════════ */
    buildList(cruise, hazard) {
        const el = document.getElementById('ship-list');
        if (!el) return;

        const cruiseGroups = this.groupCruiseShips(cruise);
        const total = cruiseGroups.length + hazard.length;

        const cnt = document.getElementById('ship-count');
        if (cnt) cnt.textContent = total + (total === 1 ? ' navio' : ' navios');

        if (!total) {
            el.innerHTML = '<div class="empty-state">Sem navios no período<br>e filtros seleccionados.</div>';
            return;
        }

        el.innerHTML = '';

        if (this.modeHazard && !this.modeCruise) el.appendChild(this.buildHazardInfoBlock());

        cruiseGroups.sort((a, b) => Math.min(...a.ships.map(s => s.date)) - Math.min(...b.ships.map(s => s.date)));

        cruiseGroups.forEach(g => {
            const card = document.createElement('div');
            const types = [...new Set(g.ships.map(s => s.type))];
            const domType = types[0];
            card.className = `ship-card ${this.CARD_CLS[domType]}`;
            card.dataset.groupKey = g.key;
            if (g.key === this.selGroupKey) card.classList.add('active');

            const first = g.ships[0];
            const tagsHtml  = types.map(t => `<span class="stag ${this.TAG_CLS[t]}">${this.TYPE_LBL[t]}</span>`).join(' ');
            const datesHtml = g.ships.map(s => `<div class="ship-date">${this.DAY_LBL(s.offset)} · ${s.hour} → ${s.to}</div>`).join('');

            card.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px">
                    <div class="ship-name">${g.name}</div>
                    <div style="display:flex;gap:3px;flex-wrap:wrap;justify-content:flex-end">${tagsHtml}</div>
                </div>
                <div class="ship-meta"><span>${first.line}</span><span>${(first.pax || 0).toLocaleString('pt-PT')} pax</span></div>
                <div class="ship-meta">${g.terminal}</div>
                ${datesHtml}`;

            card.addEventListener('click', () => {
                this.selGroupKey = this.selGroupKey === g.key ? null : g.key;
                document.querySelectorAll('.lpb-container .ship-card').forEach(c =>
                    c.classList.toggle('active', c.dataset.groupKey === this.selGroupKey)
                );
                if (this.selGroupKey) {
                    const f = this.shipFeatureMap[this.selGroupKey];
                    if (f) this.map.getView().animate({
                        center: f.getGeometry().getCoordinates(),
                        zoom: 14,
                        duration: 500
                    });
                }
                this.renderCruiseShips(cruise);
            });
            el.appendChild(card);
        });

        if (hazard.length && this.modeHazard && this.modeCruise) {
            const sep = document.createElement('div');
            sep.className = 'hz-sep';
            sep.textContent = 'Terminal Multiusos do Poço do Bispo';
            el.appendChild(sep);
        }

        hazard.sort((a, b) => a.date - b.date).forEach(s => {
            const card = document.createElement('div');
            card.className = 'ship-card t-hazard';
            card.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px">
                    <div class="ship-name">${s.name}</div>
                    <span class="stag stag-hazard">IMDG</span>
                </div>
                <div class="ship-meta"><span>${s.line}</span></div>
                <div class="ship-date">${this.DAY_LBL(s.offset)} · ${s.hour}</div>
                <div class="ship-meta" style="color:#8a1010">${s.cargo}</div>
                <div class="ship-meta">${s.from} → ${s.to}</div>`;
            el.appendChild(card);
        });
    }

    buildHazardInfoBlock() {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div class="hz-block">
                <div class="hz-block-title">Terminal Multiusos do Poço do Bispo</div>
                <div class="hz-block-sub">Frente ribeirinha oriental · Operação: APL / DGRM · Código ISPS</div>
                <div class="hz-pills">
                    <span class="hz-pill">Produtos químicos</span><span class="hz-pill">Combustíveis</span>
                    <span class="hz-pill">GNL / GPL</span><span class="hz-pill">Granéis líquidos</span>
                    <span class="hz-pill">IMDG Cl.2–9</span><span class="hz-pill">Óleos vegetais</span>
                </div>
            </div>
            <div class="hz-note">Navios IMDG <strong>não</strong> utilizam os Terminais de Cruzeiros (Santa Apolónia, Jardim do Tabaco, Rocha Conde de Óbidos, Alcântara).</div>
            <div class="hz-note">A atribuição do cais exacto depende da classe IMDG, quantidade e janela operacional autorizada.</div>
            <div class="hz-src">Fontes: <a href="https://www.portodelisboa.pt" target="_blank">portodelisboa.pt</a> · <a href="https://www.dgrm.pt" target="_blank">dgrm.pt</a></div>
            <div class="hz-sep">Navios registados</div>`;
        return wrap;
    }

    /* ═══════════════════════════════════════════════════
       UPDATE GERAL
       ═══════════════════════════════════════════════════ */
    updateView() {
        this.selGroupKey = null;
        const { cruise, hazard } = this.currentFiltered();
        this.renderCruiseShips(cruise);
        this.renderHazardShips(hazard);
        this.buildList(cruise, hazard);
    }

    /* ═══════════════════════════════════════════════════
       RELÓGIO
       ═══════════════════════════════════════════════════ */
    startClock() {
        const tick = () => {
            const n = new Date();
            const el = document.getElementById('current-time');
            if (!el) return;
            el.textContent =
                n.toLocaleTimeString('pt-PT', { hour:'2-digit', minute:'2-digit', second:'2-digit' })
                + ' · ' + n.toLocaleDateString('pt-PT');
        };
        tick();
        this.clockInterval = setInterval(tick, 1000);
    }
}
