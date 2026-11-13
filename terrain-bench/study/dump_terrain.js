// Dump cawro's course geometry for many seeds: tile angles and top-surface vertices.
//   node study/dump_terrain.js <n_seeds> > terrain.json
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
vm.runInThisContext(fs.readFileSync(path.join(__dirname, "..", "vendor", "seedrandom.js"), "utf8"));
const N = 200, W = 1.5;  // maxFloorTiles, groundPieceWidth
const out = [];
for (let s = 0; s < +process.argv[2]; s++) {
  Math.seedrandom(String(s));
  const angles = [], pts = [[-5, 0]];
  for (let k = 0; k < N; k++) {
    const a = (Math.random() * 3 - 1.5) * 1.5 * k / N;   // cw_createFloor
    angles.push(a);
    const [x, y] = pts[pts.length - 1];
    pts.push([x + W * Math.cos(a), y + W * Math.sin(a)]);
  }
  out.push({ seed: String(s), angles, pts });
}
process.stdout.write(JSON.stringify(out));
