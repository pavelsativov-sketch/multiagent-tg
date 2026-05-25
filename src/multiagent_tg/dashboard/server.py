"""HTTP-дашборд: 3D-визуализация команды + панель задач + список проектов.

Запускается отдельным процессом (CLI `multiagent-tg dashboard`). НИЧЕГО не
делает с Telegram напрямую — задачи кладутся в очередь (`bridge.TaskQueue`),
а оркестратор их читает и постит в группу от имени Светы.

Минимум зависимостей — только stdlib (http.server + threading), фронт
рисуется через Three.js с CDN.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import socketserver
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import unquote, urlparse

from multiagent_tg.bridge import StatusBoard, TaskQueue, TaskRequest
from multiagent_tg.config import AppConfig
from multiagent_tg.dashboard.projects import scan_workspace, write_index_html, write_manifest
from multiagent_tg.task_tracker import TaskTracker

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# HTML — единая страница с 3D-сценой, формой задач и списком проектов.
# --------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>multiagent-tg · team HQ</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {
  color-scheme: dark;
  --bg: #07091a;
  --panel: rgba(18, 24, 48, 0.85);
  --panel-border: #2a3565;
  --fg: #e6ecff;
  --muted: #8a93b3;
  --accent: #6366f1;
  --accent-2: #22d3ee;
  --ok: #22c55e;
  --warn: #f59e0b;
  --err: #ef4444;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; overflow: hidden;
  background: var(--bg); color: var(--fg);
  font: 13px/1.45 -apple-system, "Segoe UI", system-ui, sans-serif; }
#scene { position: absolute; inset: 0; }

.panel {
  position: absolute; z-index: 10;
  background: var(--panel); backdrop-filter: blur(8px);
  border: 1px solid var(--panel-border); border-radius: 14px;
  padding: 14px 16px; box-shadow: 0 8px 30px rgba(0,0,0,0.4);
}
#top { top: 16px; left: 16px; right: 16px; display: flex; align-items: center;
  justify-content: space-between; gap: 16px; padding: 10px 16px; }
#top h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.3px; }
#top .sub { color: var(--muted); font-size: 12px; }
#top .stats { display: flex; gap: 16px; font-size: 12px; color: var(--muted); }
#top .stats b { color: var(--fg); font-weight: 600; }

#left { top: 80px; left: 16px; width: 280px; max-height: calc(100% - 110px);
  overflow: auto; }
#left h2 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--muted); }
.agent-row { display: flex; align-items: center; gap: 10px;
  padding: 8px; border-radius: 8px; cursor: pointer;
  border: 1px solid transparent; }
.agent-row:hover { background: rgba(99, 102, 241, 0.12); border-color: var(--accent); }
.agent-row.selected { background: rgba(99, 102, 241, 0.2); border-color: var(--accent); }
.dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  box-shadow: 0 0 8px currentColor; }
.dot.idle { color: #60a5fa; background: #60a5fa; }
.dot.thinking { color: var(--warn); background: var(--warn); animation: pulse 1s infinite; }
.dot.typing { color: var(--ok); background: var(--ok); animation: pulse 0.6s infinite; }
.dot.error { color: var(--err); background: var(--err); }
.dot.offline { color: #475569; background: #475569; box-shadow: none; }
@keyframes pulse { 50% { opacity: 0.35; } }
.agent-meta { flex: 1; min-width: 0; }
.agent-meta .nm { font-weight: 600; }
.agent-meta .rl { color: var(--muted); font-size: 11px; }

#right { top: 80px; right: 16px; width: 320px; max-height: calc(100% - 110px);
  overflow: auto; }
#right h2 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--muted); display: flex; justify-content: space-between; }
#right h2 a { color: var(--accent-2); text-decoration: none; font-size: 11px; }
.proj { padding: 8px 10px; border-radius: 8px; border: 1px solid #25305a;
  margin-bottom: 8px; background: rgba(10, 14, 32, 0.6); }
.proj .h { display: flex; justify-content: space-between; gap: 8px; }
.proj .h .nm { font-weight: 600; }
.proj .h .tm { color: var(--muted); font-size: 11px; }
.proj .files { color: var(--muted); font-size: 11px; margin-top: 4px; }
.proj a.open { color: var(--accent-2); font-size: 11px; text-decoration: none; }
.proj a.open:hover { text-decoration: underline; }

#bottom { bottom: 16px; left: 16px; right: 16px; padding: 12px 14px;
  display: flex; gap: 10px; align-items: flex-end; }
#bottom .target { display: flex; flex-direction: column; gap: 4px;
  min-width: 160px; }
#bottom label { font-size: 11px; color: var(--muted); }
#bottom select, #bottom textarea {
  background: #0a1027; color: var(--fg);
  border: 1px solid #2a3565; border-radius: 8px;
  padding: 8px 10px; font: inherit; outline: none;
}
#bottom select:focus, #bottom textarea:focus { border-color: var(--accent); }
#bottom .ta { flex: 1; display: flex; flex-direction: column; gap: 4px; }
#bottom textarea { resize: none; height: 64px; }
#bottom button {
  background: var(--accent); color: white; border: none;
  padding: 12px 18px; border-radius: 10px; font-weight: 600; cursor: pointer;
  height: 64px; min-width: 110px;
}
#bottom button:hover { background: #4f53d8; }
#bottom button:disabled { opacity: 0.5; cursor: progress; }

#toast { position: absolute; bottom: 120px; left: 50%; transform: translateX(-50%);
  background: rgba(34, 197, 94, 0.95); color: white; padding: 10px 16px;
  border-radius: 10px; opacity: 0; transition: opacity 0.3s;
  pointer-events: none; z-index: 20; font-weight: 600; }
#toast.show { opacity: 1; }
#toast.err { background: rgba(239, 68, 68, 0.95); }

.empty { color: var(--muted); padding: 8px; font-style: italic; font-size: 12px; }

/* Tabs */
.tab-header { display: flex; align-items: center; gap: 12px; }
.tab { cursor: pointer; opacity: 0.5; transition: opacity 0.2s; user-select: none; }
.tab:hover { opacity: 0.8; }
.tab.active { opacity: 1; border-bottom: 2px solid var(--accent); padding-bottom: 2px; }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Current task */
.current-task {
  background: rgba(99, 102, 241, 0.15); border: 1px solid var(--accent);
  border-radius: 10px; padding: 10px; margin-bottom: 10px;
}
.ct-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
  color: var(--accent); font-weight: 700; margin-bottom: 4px; }
.ct-text { font-size: 12px; line-height: 1.4; max-height: 48px; overflow: hidden; }
.ct-agent { font-size: 11px; color: var(--muted); margin-top: 4px; }

/* Task history */
.task-item { padding: 8px 10px; border-radius: 8px; border: 1px solid #25305a;
  margin-bottom: 6px; background: rgba(10, 14, 32, 0.6); }
.task-item .t-header { display: flex; justify-content: space-between; align-items: center; }
.task-item .t-text { font-size: 12px; line-height: 1.3; max-height: 36px; overflow: hidden; }
.task-item .t-meta { color: var(--muted); font-size: 11px; margin-top: 3px; }
.task-item .status-badge {
  font-size: 10px; padding: 2px 6px; border-radius: 4px;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
}
.status-badge.created { background: #374151; color: #9ca3af; }
.status-badge.assigned { background: rgba(99, 102, 241, 0.2); color: var(--accent); }
.status-badge.in_progress { background: rgba(245, 158, 11, 0.2); color: var(--warn); }
.status-badge.review { background: rgba(34, 211, 238, 0.2); color: var(--accent-2); }
.status-badge.done { background: rgba(34, 197, 94, 0.2); color: var(--ok); }
.status-badge.failed { background: rgba(239, 68, 68, 0.2); color: var(--err); }

/* Connection lines canvas */
#connections { position: absolute; inset: 0; z-index: 5; pointer-events: none; }
</style>
</head>
<body>
<div id="scene"></div>

<div id="top" class="panel">
  <div>
    <h1>multiagent-tg · team HQ</h1>
    <div class="sub">кликни по аватарке — выдай задачу. Света раздаёт остальным.</div>
  </div>
  <div class="stats">
    <span>обработано: <b id="stat-processed">—</b></span>
    <span>активных: <b id="stat-active">0</b></span>
    <span>обновлено: <b id="stat-updated">—</b></span>
  </div>
</div>

<div id="left" class="panel">
  <h2>Команда</h2>
  <div id="agents"></div>
</div>

<div id="right" class="panel">
  <h2 class="tab-header">
    <span class="tab active" data-tab="tasks-tab">Задачи</span>
    <span class="tab" data-tab="projects-tab">Проекты</span>
    <a href="/workspace/index.html" target="_blank" style="margin-left:auto">все →</a>
  </h2>
  <div id="tasks-tab" class="tab-content active">
    <div id="current-task" class="current-task" style="display:none">
      <div class="ct-label">сейчас</div>
      <div class="ct-text" id="ct-text"></div>
      <div class="ct-agent" id="ct-agent"></div>
    </div>
    <div id="task-history"></div>
  </div>
  <div id="projects-tab" class="tab-content">
    <div id="projects"></div>
  </div>
</div>

<div id="bottom" class="panel">
  <div class="target">
    <label for="target">кому</label>
    <select id="target">
      <option value="">Свете (она распределит)</option>
    </select>
  </div>
  <div class="ta">
    <label for="text">задача</label>
    <textarea id="text" placeholder="Например: Сделай лендинг для кофейни с формой заявки. Палитра тёплая, шрифт sans-serif."></textarea>
  </div>
  <button id="send">Отправить →</button>
</div>

<div id="toast"></div>

<script type="importmap">
{ "imports": {
    "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// ---------------- 3D scene ----------------
const sceneEl = document.getElementById('scene');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x07091a);
scene.fog = new THREE.Fog(0x07091a, 14, 40);

const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.1, 100);
camera.position.set(0, 7, 12);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.shadowMap.enabled = true;
sceneEl.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 1.2, 0);
controls.enableDamping = true;
controls.minDistance = 6;
controls.maxDistance = 22;
controls.maxPolarAngle = Math.PI / 2 - 0.05;

// Lights
scene.add(new THREE.AmbientLight(0x6677aa, 0.45));
const key = new THREE.DirectionalLight(0xffffff, 0.9);
key.position.set(6, 10, 6); key.castShadow = true;
key.shadow.mapSize.set(1024, 1024);
scene.add(key);
const rim = new THREE.PointLight(0x6366f1, 1.2, 30);
rim.position.set(-5, 4, -5); scene.add(rim);
const rim2 = new THREE.PointLight(0x22d3ee, 0.8, 25);
rim2.position.set(5, 3, -3); scene.add(rim2);

// Floor — glowing grid disk
const floor = new THREE.Mesh(
  new THREE.CircleGeometry(10, 64),
  new THREE.MeshStandardMaterial({ color: 0x0d1230, roughness: 0.7, metalness: 0.2 })
);
floor.rotation.x = -Math.PI / 2;
floor.receiveShadow = true;
scene.add(floor);
const grid = new THREE.GridHelper(20, 20, 0x2a3565, 0x1a2150);
grid.position.y = 0.01;
scene.add(grid);

// Central table (where projects "live")
const table = new THREE.Mesh(
  new THREE.CylinderGeometry(1.4, 1.4, 0.2, 32),
  new THREE.MeshStandardMaterial({ color: 0x1a2150, emissive: 0x6366f1,
    emissiveIntensity: 0.25, roughness: 0.4 })
);
table.position.y = 0.6;
table.castShadow = true; table.receiveShadow = true;
scene.add(table);
const tableLeg = new THREE.Mesh(
  new THREE.CylinderGeometry(0.15, 0.15, 0.6, 16),
  new THREE.MeshStandardMaterial({ color: 0x25305a })
);
tableLeg.position.y = 0.3; scene.add(tableLeg);

// Floating "hub" sphere above the table — visual focus
const hub = new THREE.Mesh(
  new THREE.IcosahedronGeometry(0.35, 1),
  new THREE.MeshStandardMaterial({ color: 0x22d3ee, emissive: 0x22d3ee,
    emissiveIntensity: 0.7, wireframe: true })
);
hub.position.y = 1.4; scene.add(hub);

// ---------------- Agent avatars ----------------
const ROLE_COLORS = {
  director: 0xf472b6,
  dev:      0x22d3ee,
  designer: 0xa78bfa,
  qa:       0xfbbf24,
  default:  0x60a5fa,
};
const STATE_EMISSIVE = {
  idle:     0x113355,
  thinking: 0xf59e0b,
  typing:   0x22c55e,
  error:    0xef4444,
  offline:  0x0a0a0a,
};

// Per-character appearance
const CHAR_LOOKS = {
  sveta: { skin: 0xf5d0b0, hair: 0xd4a574, hairStyle: 'long', outfit: 0xf472b6,
           outfitAccent: 0xdb2777, skirtColor: 0x1e1b4b, accessory: 'clipboard',
           eyeColor: 0x3b82f6, lipColor: 0xe11d48 },
  igor:  { skin: 0xe8c8a0, hair: 0x4a3728, hairStyle: 'short', outfit: 0x1e3a5f,
           outfitAccent: 0x22d3ee, skirtColor: null, accessory: 'laptop',
           eyeColor: 0x4b5563, lipColor: null },
  anya:  { skin: 0xf5d0b0, hair: 0xfbbf24, hairStyle: 'ponytail', outfit: 0x7c3aed,
           outfitAccent: 0xa78bfa, skirtColor: 0x4c1d95, accessory: 'palette',
           eyeColor: 0x10b981, lipColor: 0xec4899 },
  kostya:{ skin: 0xe0c0a0, hair: 0x78350f, hairStyle: 'buzz', outfit: 0x365314,
           outfitAccent: 0xfbbf24, skirtColor: null, accessory: 'magnifier',
           eyeColor: 0x92400e, lipColor: null },
};
const DEFAULT_LOOK = { skin: 0xf0c8a0, hair: 0x6b4423, hairStyle: 'short', outfit: 0x3b82f6,
  outfitAccent: 0x60a5fa, skirtColor: null, accessory: null, eyeColor: 0x4b5563, lipColor: null };

function makeLabelSprite(text, roleColor) {
  const c = document.createElement('canvas');
  c.width = 512; c.height = 128;
  const ctx = c.getContext('2d');
  // Background pill
  ctx.fillStyle = 'rgba(10, 14, 32, 0.9)';
  ctx.strokeStyle = '#' + (roleColor || 0x6366f1).toString(16).padStart(6,'0');
  ctx.lineWidth = 4;
  roundRect(ctx, 8, 8, 496, 112, 24); ctx.fill(); ctx.stroke();
  // Glow
  ctx.shadowColor = ctx.strokeStyle; ctx.shadowBlur = 12;
  ctx.fillStyle = '#f0f4ff';
  ctx.font = 'bold 48px -apple-system, Segoe UI, system-ui';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(text, 256, 64);
  const tex = new THREE.CanvasTexture(c);
  tex.anisotropy = 4;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sp = new THREE.Sprite(mat);
  sp.scale.set(2.4, 0.6, 1);
  return sp;
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x+r, y);
  ctx.arcTo(x+w, y,   x+w, y+h, r);
  ctx.arcTo(x+w, y+h, x,   y+h, r);
  ctx.arcTo(x,   y+h, x,   y,   r);
  ctx.arcTo(x,   y,   x+w, y,   r);
  ctx.closePath();
}

const avatars = [];

function makeAvatar(agent, angle) {
  const color = ROLE_COLORS[agent.role] || ROLE_COLORS.default;
  const look = CHAR_LOOKS[agent.name] || DEFAULT_LOOK;
  const g = new THREE.Group();

  const skinMat = new THREE.MeshStandardMaterial({
    color: look.skin, roughness: 0.6, metalness: 0.05 });
  const outfitMat = new THREE.MeshStandardMaterial({
    color: look.outfit, emissive: STATE_EMISSIVE.idle, emissiveIntensity: 0.3,
    roughness: 0.4, metalness: 0.2 });
  const outfitAccentMat = new THREE.MeshStandardMaterial({
    color: look.outfitAccent, roughness: 0.4, metalness: 0.3 });
  const hairMat = new THREE.MeshStandardMaterial({
    color: look.hair, roughness: 0.8, metalness: 0.05 });

  // --- LEGS ---
  for (const lx of [-0.15, 0.15]) {
    // Upper leg
    const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.08, 0.55, 8),
      new THREE.MeshStandardMaterial({ color: look.skirtColor || 0x1e293b, roughness: 0.5 }));
    leg.position.set(lx, 0.38, 0);
    g.add(leg);
    // Shoe
    const shoe = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.1, 0.22),
      new THREE.MeshStandardMaterial({ color: 0x1a1a2e, roughness: 0.3, metalness: 0.4 }));
    shoe.position.set(lx, 0.08, 0.04);
    g.add(shoe);
  }

  // --- TORSO ---
  // Main body
  const torso = new THREE.Mesh(
    new THREE.CylinderGeometry(0.28, 0.22, 0.65, 12), outfitMat);
  torso.position.y = 0.95;
  torso.castShadow = true;
  g.add(torso);

  // Shoulders
  const shoulders = new THREE.Mesh(
    new THREE.BoxGeometry(0.7, 0.12, 0.32), outfitMat);
  shoulders.position.y = 1.22;
  g.add(shoulders);

  // Collar / accent stripe
  const collar = new THREE.Mesh(
    new THREE.TorusGeometry(0.22, 0.04, 6, 16), outfitAccentMat);
  collar.position.y = 1.3;
  collar.rotation.x = Math.PI / 2;
  g.add(collar);

  // Skirt (for female characters)
  if (look.skirtColor) {
    const skirt = new THREE.Mesh(
      new THREE.CylinderGeometry(0.18, 0.35, 0.35, 12),
      new THREE.MeshStandardMaterial({ color: look.skirtColor, roughness: 0.5 }));
    skirt.position.y = 0.62;
    g.add(skirt);
  }

  // --- ARMS ---
  for (const side of [-1, 1]) {
    // Upper arm
    const upperArm = new THREE.Mesh(
      new THREE.CylinderGeometry(0.06, 0.055, 0.35, 8), outfitMat);
    upperArm.position.set(side * 0.4, 1.1, 0);
    upperArm.rotation.z = side * 0.25;
    g.add(upperArm);
    // Lower arm (skin)
    const forearm = new THREE.Mesh(
      new THREE.CylinderGeometry(0.05, 0.04, 0.3, 8), skinMat);
    forearm.position.set(side * 0.48, 0.85, 0.05);
    forearm.rotation.z = side * 0.15;
    g.add(forearm);
    // Hand
    const hand = new THREE.Mesh(
      new THREE.SphereGeometry(0.05, 8, 8), skinMat);
    hand.position.set(side * 0.5, 0.7, 0.08);
    g.add(hand);
  }

  // --- NECK ---
  const neck = new THREE.Mesh(
    new THREE.CylinderGeometry(0.08, 0.1, 0.12, 8), skinMat);
  neck.position.y = 1.38;
  g.add(neck);

  // --- HEAD ---
  const headGroup = new THREE.Group();
  headGroup.position.y = 1.55;

  const head = new THREE.Mesh(
    new THREE.SphereGeometry(0.28, 24, 18), skinMat);
  head.castShadow = true;
  headGroup.add(head);

  // --- FACE ---
  // Eyes
  const eyeWhiteMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
  const irisMat = new THREE.MeshBasicMaterial({ color: look.eyeColor });
  const pupilMat = new THREE.MeshBasicMaterial({ color: 0x0a0a15 });
  for (const ex of [-0.09, 0.09]) {
    // Eye white
    const eyeWhite = new THREE.Mesh(new THREE.SphereGeometry(0.045, 12, 8), eyeWhiteMat);
    eyeWhite.position.set(ex, 0.04, 0.24);
    headGroup.add(eyeWhite);
    // Iris
    const iris = new THREE.Mesh(new THREE.SphereGeometry(0.028, 10, 8), irisMat);
    iris.position.set(ex, 0.04, 0.27);
    headGroup.add(iris);
    // Pupil
    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.015, 8, 8), pupilMat);
    pupil.position.set(ex, 0.04, 0.28);
    headGroup.add(pupil);
    // Eyelid crease
    const brow = new THREE.Mesh(new THREE.BoxGeometry(0.07, 0.012, 0.02),
      new THREE.MeshStandardMaterial({ color: look.hair, roughness: 1 }));
    brow.position.set(ex, 0.085, 0.25);
    brow.rotation.x = -0.15;
    headGroup.add(brow);
  }
  // Nose
  const nose = new THREE.Mesh(
    new THREE.ConeGeometry(0.025, 0.06, 6),
    new THREE.MeshStandardMaterial({ color: look.skin, roughness: 0.7 }));
  nose.position.set(0, -0.01, 0.28);
  nose.rotation.x = -0.3;
  headGroup.add(nose);
  // Mouth
  if (look.lipColor) {
    const lip = new THREE.Mesh(new THREE.TorusGeometry(0.035, 0.012, 4, 12),
      new THREE.MeshStandardMaterial({ color: look.lipColor, roughness: 0.4 }));
    lip.position.set(0, -0.07, 0.24);
    lip.rotation.x = 0.2;
    headGroup.add(lip);
  } else {
    const mouth = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.01, 0.01),
      new THREE.MeshStandardMaterial({ color: 0xc4756e, roughness: 0.5 }));
    mouth.position.set(0, -0.07, 0.27);
    headGroup.add(mouth);
  }
  // Ears
  for (const side of [-1, 1]) {
    const ear = new THREE.Mesh(new THREE.SphereGeometry(0.04, 8, 6), skinMat);
    ear.position.set(side * 0.27, 0, 0);
    ear.scale.set(0.6, 1, 0.7);
    headGroup.add(ear);
  }

  // --- HAIR ---
  if (look.hairStyle === 'long') {
    // Long flowing hair (Света)
    const hairTop = new THREE.Mesh(
      new THREE.SphereGeometry(0.3, 16, 12, 0, Math.PI * 2, 0, Math.PI * 0.6), hairMat);
    hairTop.position.set(0, 0.05, -0.02);
    headGroup.add(hairTop);
    // Back hair flowing down
    const hairBack = new THREE.Mesh(
      new THREE.CylinderGeometry(0.22, 0.15, 0.55, 10), hairMat);
    hairBack.position.set(0, -0.25, -0.12);
    headGroup.add(hairBack);
    // Side strands
    for (const side of [-1, 1]) {
      const strand = new THREE.Mesh(
        new THREE.CylinderGeometry(0.06, 0.04, 0.4, 6), hairMat);
      strand.position.set(side * 0.22, -0.15, 0.05);
      strand.rotation.z = side * 0.15;
      headGroup.add(strand);
    }
  } else if (look.hairStyle === 'ponytail') {
    // Ponytail (Аня)
    const hairTop = new THREE.Mesh(
      new THREE.SphereGeometry(0.3, 16, 12, 0, Math.PI * 2, 0, Math.PI * 0.55), hairMat);
    hairTop.position.set(0, 0.04, 0);
    headGroup.add(hairTop);
    // Ponytail
    const tail = new THREE.Mesh(
      new THREE.CylinderGeometry(0.08, 0.05, 0.5, 8), hairMat);
    tail.position.set(0, 0.02, -0.28);
    tail.rotation.x = 0.8;
    headGroup.add(tail);
    // Hair tie
    const tie = new THREE.Mesh(
      new THREE.TorusGeometry(0.08, 0.02, 6, 12),
      new THREE.MeshStandardMaterial({ color: 0xec4899 }));
    tie.position.set(0, 0.08, -0.25);
    tie.rotation.x = 0.6;
    headGroup.add(tie);
    // Bangs
    const bangs = new THREE.Mesh(
      new THREE.BoxGeometry(0.35, 0.06, 0.12), hairMat);
    bangs.position.set(0, 0.16, 0.2);
    bangs.rotation.x = -0.3;
    headGroup.add(bangs);
  } else if (look.hairStyle === 'buzz') {
    // Buzz cut (Костя)
    const buzz = new THREE.Mesh(
      new THREE.SphereGeometry(0.29, 16, 12, 0, Math.PI * 2, 0, Math.PI * 0.55), hairMat);
    buzz.position.set(0, 0.02, 0);
    headGroup.add(buzz);
  } else {
    // Short styled hair (Игорь)
    const hairTop = new THREE.Mesh(
      new THREE.SphereGeometry(0.3, 16, 12, 0, Math.PI * 2, 0, Math.PI * 0.5), hairMat);
    hairTop.position.set(0, 0.04, 0.02);
    headGroup.add(hairTop);
    // Side part
    const sidePart = new THREE.Mesh(
      new THREE.BoxGeometry(0.15, 0.06, 0.25), hairMat);
    sidePart.position.set(-0.15, 0.12, 0.05);
    sidePart.rotation.z = 0.2;
    headGroup.add(sidePart);
  }

  g.add(headGroup);

  // --- ACCESSORY ---
  if (look.accessory === 'clipboard') {
    // Clipboard for director
    const board = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.28, 0.02),
      new THREE.MeshStandardMaterial({ color: 0x8b5e3c, roughness: 0.7 }));
    board.position.set(-0.55, 0.85, 0.15);
    board.rotation.z = 0.3;
    g.add(board);
    const paper = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.22, 0.005),
      new THREE.MeshStandardMaterial({ color: 0xfefce8 }));
    paper.position.set(-0.55, 0.85, 0.17);
    paper.rotation.z = 0.3;
    g.add(paper);
  } else if (look.accessory === 'laptop') {
    // Laptop for dev
    const base = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.02, 0.2),
      new THREE.MeshStandardMaterial({ color: 0x374151, metalness: 0.6, roughness: 0.3 }));
    base.position.set(0, 0.72, 0.35);
    g.add(base);
    const screen = new THREE.Mesh(new THREE.BoxGeometry(0.26, 0.18, 0.01),
      new THREE.MeshStandardMaterial({ color: 0x0f172a, emissive: 0x22d3ee,
        emissiveIntensity: 0.6 }));
    screen.position.set(0, 0.82, 0.44);
    screen.rotation.x = -0.3;
    g.add(screen);
  } else if (look.accessory === 'palette') {
    // Paint palette for designer
    const palette = new THREE.Mesh(
      new THREE.CylinderGeometry(0.15, 0.15, 0.02, 12),
      new THREE.MeshStandardMaterial({ color: 0xd4a574, roughness: 0.7 }));
    palette.position.set(0.55, 0.8, 0.1);
    palette.rotation.z = -0.5;
    palette.rotation.x = 0.8;
    g.add(palette);
    // Paint dots
    const paintColors = [0xef4444, 0x3b82f6, 0xfbbf24, 0x22c55e, 0xa855f7];
    paintColors.forEach((pc, i) => {
      const dot = new THREE.Mesh(new THREE.SphereGeometry(0.025, 8, 8),
        new THREE.MeshStandardMaterial({ color: pc, roughness: 0.3 }));
      const a = (i / paintColors.length) * Math.PI * 1.2 + 0.5;
      dot.position.set(0.55 + Math.cos(a) * 0.09, 0.82, 0.1 + Math.sin(a) * 0.09);
      g.add(dot);
    });
  } else if (look.accessory === 'magnifier') {
    // Magnifying glass for QA
    const handle = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.025, 0.2, 8),
      new THREE.MeshStandardMaterial({ color: 0x78350f, roughness: 0.5 }));
    handle.position.set(0.52, 0.72, 0.18);
    handle.rotation.z = -0.7;
    g.add(handle);
    const lens = new THREE.Mesh(new THREE.TorusGeometry(0.08, 0.015, 8, 16),
      new THREE.MeshStandardMaterial({ color: 0xfbbf24, metalness: 0.7, roughness: 0.2 }));
    lens.position.set(0.48, 0.88, 0.18);
    g.add(lens);
    const glass = new THREE.Mesh(new THREE.CircleGeometry(0.07, 16),
      new THREE.MeshStandardMaterial({ color: 0xbfdbfe, transparent: true, opacity: 0.3 }));
    glass.position.set(0.48, 0.88, 0.185);
    g.add(glass);
  }

  // --- PEDESTAL ---
  const pedMat = new THREE.MeshStandardMaterial({
    color, emissive: color, emissiveIntensity: 0.5,
    transparent: true, opacity: 0.85 });
  const ped = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.65, 0.06, 32), pedMat);
  ped.position.y = 0.03; g.add(ped);
  // Ring glow
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(0.6, 0.02, 8, 32),
    new THREE.MeshStandardMaterial({ color, emissive: color,
      emissiveIntensity: 0.8, transparent: true, opacity: 0.6 }));
  ring.position.y = 0.07;
  ring.rotation.x = Math.PI / 2;
  g.add(ring);

  // --- LABEL ---
  const roleLabels = { director: 'Директор', dev: 'Разработчик', designer: 'Дизайнер', qa: 'QA' };
  const labelText = agent.display_name + (agent.role === 'director' ? ' ★' : '');
  const label = makeLabelSprite(labelText, color);
  label.position.y = 2.2; g.add(label);

  // Role sub-label
  const roleSpr = makeRoleSprite(roleLabels[agent.role] || agent.role, color);
  roleSpr.position.y = 1.95; g.add(roleSpr);

  // Place around circle
  const R = 3.8;
  g.position.set(Math.cos(angle) * R, 0, Math.sin(angle) * R);
  g.rotation.y = -angle + Math.PI / 2;
  g.userData = { name: agent.name, role: agent.role, body: torso, head: headGroup,
                 ped, ring, label, baseY: 0, t0: Math.random() * 10 };
  scene.add(g);
  return g;
}

function makeRoleSprite(text, roleColor) {
  const c = document.createElement('canvas');
  c.width = 256; c.height = 64;
  const ctx = c.getContext('2d');
  const hex = '#' + (roleColor || 0x6366f1).toString(16).padStart(6,'0');
  ctx.fillStyle = hex;
  ctx.globalAlpha = 0.7;
  ctx.font = '24px -apple-system, Segoe UI, system-ui';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(text, 128, 32);
  const tex = new THREE.CanvasTexture(c);
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sp = new THREE.Sprite(mat);
  sp.scale.set(1.4, 0.35, 1);
  return sp;
}

// ---------------- Data wiring ----------------
let AGENTS = [];   // full list from /api/status (configs)
let STATE  = {};   // name -> state object
let selectedTarget = '';

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function applyState() {
  for (const av of avatars) {
    const st = STATE[av.userData.name] || {};
    const s = st.state || 'offline';
    av.userData.currentState = s;
    const emissive = STATE_EMISSIVE[s] ?? STATE_EMISSIVE.idle;
    av.userData.body.material.emissive.setHex(emissive);
    av.userData.body.material.emissiveIntensity = (s === 'idle' || s === 'offline') ? 0.25 : 0.9;
    av.userData.ped.material.emissiveIntensity = (s === 'offline') ? 0.1 : 0.6;
    if (av.userData.ring) {
      av.userData.ring.material.emissiveIntensity = (s === 'offline') ? 0.2 : 0.8;
    }
  }
}

function renderAgentsList() {
  const wrap = document.getElementById('agents');
  wrap.innerHTML = '';
  const target = document.getElementById('target');
  // refresh select options
  const cur = target.value;
  target.innerHTML = '<option value="">Свете (она распределит)</option>';
  for (const a of AGENTS) {
    const st = STATE[a.name] || {};
    const s = st.state || 'offline';

    const row = document.createElement('div');
    row.className = 'agent-row' + (selectedTarget === a.name ? ' selected' : '');
    row.innerHTML = `
      <span class="dot ${s}"></span>
      <div class="agent-meta">
        <div class="nm">${escapeHTML(a.display_name)} ${a.role==='director'?'★':''}</div>
        <div class="rl">${escapeHTML(a.role)} · ${escapeHTML(a.model || '')}</div>
      </div>
    `;
    row.onclick = () => selectTarget(a.name);
    wrap.appendChild(row);

    const opt = document.createElement('option');
    opt.value = a.name;
    opt.textContent = `${a.display_name} (${a.role})`;
    target.appendChild(opt);
  }
  target.value = selectedTarget || cur || '';
}

function renderProjects(projects) {
  const wrap = document.getElementById('projects');
  if (!projects.length) {
    wrap.innerHTML = '<div class="empty">Пока пусто. Поставьте задачу — Игорь и Аня создадут проект в workspace/.</div>';
    return;
  }
  wrap.innerHTML = projects.slice(0, 20).map(p => {
    const open = p.entry_file
      ? `<a class="open" target="_blank" href="/workspace/${encodeURI(p.path)}${p.kind==='folder'?'/'+encodeURI(p.entry_file):''}">открыть →</a>`
      : '';
    return `
    <div class="proj">
      <div class="h">
        <span class="nm">${escapeHTML(p.slug)}</span>
        <span class="tm">${humanTime(p.mtime)}</span>
      </div>
      <div class="files">${p.kind} · ${p.files.length} файл(а/ов) · ${humanSize(p.total_size)}</div>
      ${open}
    </div>`;
  }).join('');
}

function selectTarget(name) {
  selectedTarget = name === selectedTarget ? '' : name;
  document.getElementById('target').value = selectedTarget;
  renderAgentsList();
  // Camera nudge toward selected avatar
  const av = avatars.find(a => a.userData.name === selectedTarget);
  if (av) {
    const p = av.position;
    controls.target.lerp(new THREE.Vector3(p.x*0.4, 1.3, p.z*0.4), 0.5);
  }
}

function toast(msg, isErr=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.toggle('err', isErr);
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2200);
}

document.getElementById('send').onclick = async () => {
  const textEl = document.getElementById('text');
  const text = textEl.value.trim();
  if (!text) { toast('Напишите задачу', true); return; }
  const target = document.getElementById('target').value || null;
  const btn = document.getElementById('send'); btn.disabled = true;
  btn.textContent = '⏳';
  try {
    const res = await fetchJSON('/api/tasks', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text, target_agent: target }),
    });
    textEl.value = '';
    const agentName = target || 'sveta';
    toast(target ? `✓ Задача → ${target}` : '✓ Задача → Света распределит');
    // Show connection animation from director to target
    if (target && target !== 'sveta') showConnection('sveta', target);
    // Switch to tasks tab
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="tasks-tab"]')?.classList.add('active');
    document.getElementById('tasks-tab')?.classList.add('active');
    // Force immediate poll
    setTimeout(poll, 300);
  } catch (e) { toast('Ошибка: ' + e.message, true); }
  finally { btn.disabled = false; btn.textContent = 'Отправить'; }
};
document.getElementById('text').addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') document.getElementById('send').click();
});

// Click on 3D avatar -> select
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();
renderer.domElement.addEventListener('click', (ev) => {
  const r = renderer.domElement.getBoundingClientRect();
  mouse.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
  mouse.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hit = raycaster.intersectObjects(avatars, true)[0];
  if (hit) {
    let g = hit.object;
    while (g && !g.userData?.name) g = g.parent;
    if (g) selectTarget(g.userData.name);
  }
});

// ---------------- Task history ----------------
function renderTaskHistory(data) {
  // Task list
  const wrap = document.getElementById('task-history');
  const tasks = data.tasks || [];
  if (!tasks.length) {
    wrap.innerHTML = '<div class="empty">Задач пока нет. Отправьте первую через форму ниже.</div>';
    return;
  }
  wrap.innerHTML = tasks.slice(0, 20).map(t => {
    const status = t.status || 'created';
    const statusLabels = {
      created: 'новая', assigned: 'назначена', in_progress: 'в работе',
      review: 'ревью', done: 'готово', failed: 'ошибка'
    };
    const agentInfo = t.assigned_to ? ` → ${escapeHTML(t.assigned_to)}` : '';
    const results = (t.results||[]).length ? ` · ${t.results.length} результат(ов)` : '';
    return `
    <div class="task-item">
      <div class="t-header">
        <span class="t-text">${escapeHTML(t.text.substring(0, 100))}</span>
        <span class="status-badge ${status}">${statusLabels[status]||status}</span>
      </div>
      <div class="t-meta">${t.source}${agentInfo}${results} · ${humanTime(t.created_at)}</div>
    </div>`;
  }).join('');
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const targetId = tab.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(targetId)?.classList.add('active');
  });
});

// ---------------- Poll loop ----------------
async function poll() {
  try {
    const s = await fetchJSON('/api/status');
    if (!AGENTS.length) {
      AGENTS = s.agents_config;
      const n = AGENTS.length;
      AGENTS.forEach((a, i) => {
        const angle = (i / n) * Math.PI * 2 - Math.PI / 2;
        avatars.push(makeAvatar(a, angle));
      });
    }
    STATE = s.state.agents || {};
    document.getElementById('stat-processed').textContent = s.state.tasks_processed ?? 0;
    document.getElementById('stat-updated').textContent =
      s.state.updated_at ? humanTime(s.state.updated_at) : '—';
    applyState();
    renderAgentsList();

    // Render current task from status
    const ct = s.state.current_task;
    if (ct) {
      const ctEl = document.getElementById('current-task');
      ctEl.style.display = '';
      document.getElementById('ct-text').textContent = ct.text || '';
      document.getElementById('ct-agent').textContent = ct.assigned_to
        ? `→ ${ct.assigned_to}` : 'Света распределяет...';
    } else {
      document.getElementById('current-task').style.display = 'none';
    }
  } catch (e) { console.warn('status poll:', e); }

  try {
    const p = await fetchJSON('/api/projects');
    renderProjects(p.projects);
  } catch (e) { console.warn('projects poll:', e); }

  try {
    const th = await fetchJSON('/api/tasks/history');
    renderTaskHistory(th);
    document.getElementById('stat-active').textContent = th.active ?? 0;
  } catch (e) { console.warn('tasks poll:', e); }
}
poll();
setInterval(poll, 2000);

// ---------------- Connection lines (task delegation) ----------------
const connectionLines = [];
const lineMat = new THREE.LineBasicMaterial({
  color: 0x6366f1, transparent: true, opacity: 0.6, linewidth: 2
});

function showConnection(fromName, toName) {
  const from = avatars.find(a => a.userData.name === fromName);
  const to = avatars.find(a => a.userData.name === toName);
  if (!from || !to) return;
  const pts = [
    new THREE.Vector3(from.position.x, 1.5, from.position.z),
    new THREE.Vector3(0, 2.0, 0),  // route through hub
    new THREE.Vector3(to.position.x, 1.5, to.position.z),
  ];
  const curve = new THREE.QuadraticBezierCurve3(pts[0], pts[1], pts[2]);
  const geo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(20));
  const line = new THREE.Line(geo, lineMat.clone());
  line.userData.createdAt = performance.now();
  line.userData.lifetime = 4000;
  scene.add(line);
  connectionLines.push(line);
}

function updateConnections() {
  const now = performance.now();
  for (let i = connectionLines.length - 1; i >= 0; i--) {
    const line = connectionLines[i];
    const age = now - line.userData.createdAt;
    if (age > line.userData.lifetime) {
      scene.remove(line);
      line.geometry.dispose();
      line.material.dispose();
      connectionLines.splice(i, 1);
    } else {
      const progress = age / line.userData.lifetime;
      line.material.opacity = 0.6 * (1 - progress);
    }
  }
}

// Particles floating around hub
const particleCount = 40;
const particleGeo = new THREE.BufferGeometry();
const particlePositions = new Float32Array(particleCount * 3);
for (let i = 0; i < particleCount; i++) {
  const angle = Math.random() * Math.PI * 2;
  const r = 1.5 + Math.random() * 2;
  particlePositions[i*3] = Math.cos(angle) * r;
  particlePositions[i*3+1] = 0.8 + Math.random() * 2;
  particlePositions[i*3+2] = Math.sin(angle) * r;
}
particleGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));
const particleMat = new THREE.PointsMaterial({
  color: 0x6366f1, size: 0.06, transparent: true, opacity: 0.5,
  blending: THREE.AdditiveBlending, depthWrite: false
});
const particles = new THREE.Points(particleGeo, particleMat);
scene.add(particles);

// ---------------- Animation ----------------
const clock = new THREE.Clock();
function animate() {
  const t = clock.getElapsedTime();
  hub.rotation.x = t * 0.6; hub.rotation.y = t * 0.8;
  hub.position.y = 1.4 + Math.sin(t * 1.5) * 0.1;

  // Particles rotation
  particles.rotation.y = t * 0.15;
  const pos = particles.geometry.attributes.position;
  for (let i = 0; i < particleCount; i++) {
    pos.array[i*3+1] += Math.sin(t * 2 + i) * 0.002;
  }
  pos.needsUpdate = true;

  for (const av of avatars) {
    const st = av.userData.currentState || 'offline';
    const local = t + av.userData.t0;
    av.position.y = Math.sin(local * 1.2) * 0.04;
    if (st === 'thinking') {
      av.userData.head.rotation.y = local * 1.8;
      av.userData.body.material.emissiveIntensity = 0.5 + 0.5 * Math.abs(Math.sin(local * 3));
      if (av.userData.ring) av.userData.ring.rotation.z = local * 2;
    } else if (st === 'typing') {
      av.position.y += Math.abs(Math.sin(local * 5)) * 0.12;
      av.userData.body.material.emissiveIntensity = 0.6 + 0.3 * Math.sin(local * 5);
    } else if (st === 'error') {
      av.userData.body.material.emissiveIntensity = 0.5 + 0.5 * Math.sin(local * 8);
    } else {
      av.userData.head.rotation.y = Math.sin(local * 0.6) * 0.2;
    }
  }

  updateConnections();
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// utils
function escapeHTML(s) { return (s||'').replace(/[&<>"]/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function humanTime(ts) {
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function humanSize(n) {
  for (const u of ['B','KB','MB','GB']) {
    if (n < 1024) return u==='B' ? `${n|0} ${u}` : `${n.toFixed(1)} ${u}`;
    n /= 1024;
  }
  return `${n.toFixed(1)} TB`;
}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "multiagent-tg-dashboard/1.0"
    # Set by ThreadedServer
    config: AppConfig
    task_queue: TaskQueue
    status_board: StatusBoard
    task_tracker: TaskTracker

    # quieter logs
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - - %s", self.address_string(), format % args)

    # ------------- helpers -------------
    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ------------- routes -------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/index.html"):
            self._send_bytes(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            agents_cfg = [
                {
                    "name": a.name,
                    "display_name": a.display_name,
                    "role": a.role,
                    "model": a.model,
                    "enabled": a.enabled,
                    "tools": a.tools,
                }
                for a in self.config.all_agents
            ]
            self._send_json(200, {
                "agents_config": agents_cfg,
                "state": self.status_board.snapshot(),
            })
            return

        if path == "/api/projects":
            projects = scan_workspace(self.config.workspace_dir)
            try:
                write_manifest(self.config.workspace_dir, projects)
                write_index_html(self.config.workspace_dir, projects)
            except Exception as e:
                log.warning("Не удалось обновить hub-индекс: %s", e)
            self._send_json(200, {
                "projects": [p.to_dict() for p in projects],
                "generated_at": time.time(),
            })
            return

        if path == "/api/tasks/history":
            self._send_json(200, self.task_tracker.snapshot())
            return

        if path.startswith("/workspace/"):
            self._serve_workspace(path[len("/workspace/"):])
            return

        self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/tasks":
            data = self._read_json()
            text = (data.get("text") or "").strip()
            target = (data.get("target_agent") or "").strip() or None
            if not text:
                self._send_json(400, {"error": "empty text"})
                return
            if len(text) > 4000:
                self._send_json(400, {"error": "text too long (max 4000 chars)"})
                return
            if target and target not in {a.name for a in self.config.all_agents}:
                self._send_json(400, {"error": f"unknown agent: {target}"})
                return
            task = TaskRequest(
                id=f"{int(time.time()*1000)}-{id(self) & 0xFFFF:x}",
                text=text,
                target_agent=target,
                source="dashboard",
            )
            self.task_queue.append(task)
            # Also track the task immediately for dashboard visibility
            self.task_tracker.create(task.id, text, source="dashboard")
            if target:
                self.task_tracker.assign(task.id, target)
            self.status_board.set_current_task(text, assigned_to=target)
            log.info("Задача из дашборда: id=%s target=%s len=%d", task.id, target or "auto", len(text))
            self._send_json(200, {"ok": True, "task": asdict(task)})
            return

        self._send_json(404, {"error": "not found", "path": path})

    # ------------- workspace static -------------
    def _serve_workspace(self, rel: str) -> None:
        if not rel:
            rel = "index.html"
        ws = self.config.workspace_dir
        # block path traversal
        target = (ws / rel).resolve()
        try:
            target.relative_to(ws.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.exists():
            self._send_json(404, {"error": "not found", "rel": rel})
            return
        if target.is_dir():
            idx = target / "index.html"
            if idx.exists():
                target = idx
            else:
                # auto-listing
                items = sorted(target.iterdir())
                links = "".join(
                    f'<li><a href="{p.name}{"/" if p.is_dir() else ""}">{p.name}{"/" if p.is_dir() else ""}</a></li>'
                    for p in items
                )
                html = (
                    "<!doctype html><meta charset=utf-8>"
                    f"<title>{rel}</title><h1>{rel}</h1><ul>{links}</ul>"
                )
                self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
        ctype, _ = mimetypes.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        data = target.read_bytes()
        self._send_bytes(200, data, ctype)


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(config: AppConfig, host: str | None = None, port: int | None = None) -> None:
    """Запустить дашборд (блокирующий вызов)."""
    host = host or config.dashboard_host
    port = port or config.dashboard_port

    task_queue = TaskQueue(config.data_dir)
    status_board = StatusBoard(config.data_dir)
    task_tracker = TaskTracker(config.data_dir)

    # Bind shared state to handler via closure subclass.
    handler_cls = type("BoundHandler", (_Handler,), {
        "config": config,
        "task_queue": task_queue,
        "status_board": status_board,
        "task_tracker": task_tracker,
    })

    # Регенерируем hub при старте — чтобы /workspace/index.html был свежим.
    try:
        projects = scan_workspace(config.workspace_dir)
        write_manifest(config.workspace_dir, projects)
        write_index_html(config.workspace_dir, projects)
    except Exception as e:
        log.warning("Не смог сгенерировать hub-индекс на старте: %s", e)

    server = ThreadedServer((host, port), handler_cls)
    url = f"http://{host}:{port}/"
    log.info("Дашборд: %s  (Ctrl+C — выход)", url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_in_background(config: AppConfig) -> threading.Thread:
    """Запустить дашборд в фоне (для интеграции с `run`)."""
    th = threading.Thread(target=serve, args=(config,), daemon=True, name="dashboard")
    th.start()
    return th
