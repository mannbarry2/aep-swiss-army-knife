#!/usr/bin/env node
/*
 * AJO Journey Exporter
 * --------------------
 * Pulls Adobe Journey Optimizer journey definitions (every node/step, i.e. the
 * same content as the UI's "Copy technical details") straight from the AJO
 * authoring API - the internal endpoint the AJO canvas itself calls.
 *
 *   1. Lists every journey in the sandbox (paginated).
 *   2. Fetches each journey's latest version definition.
 *   3. Saves the raw JSON per journey, plus a summary index.csv of nodes.
 *
 * No dependencies - just Node 18+ (uses built-in fetch).
 *
 * ⚠️  UNSUPPORTED ENDPOINT. journey-private.adobe.io/authoring is the private API
 *     behind the AJO UI. It is not an official/public API and can change without
 *     notice. Use for reporting/inspection only; do not build critical automation
 *     on it. Read-only here (GET only) - it never modifies a journey.
 *
 * USAGE
 *   export AJO_BEARER_TOKEN="eyJ..."                     # required (see README)
 *   export AJO_ORG_ID="XXXXXXXXXXXX@AdobeOrg"            # required
 *   export AJO_SANDBOX="prod"                            # optional (default: prod)
 *   node ajo-journey-export.mjs                          # export ALL journeys
 *   node ajo-journey-export.mjs --journey <journeyUid>   # just one journey
 *   node ajo-journey-export.mjs --out ./my-dir           # output dir (default ./ajo-journeys)
 */

import { writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

// ---- config -------------------------------------------------------------
const TOKEN   = process.env.AJO_BEARER_TOKEN
const ORG_ID  = process.env.AJO_ORG_ID
const SANDBOX = process.env.AJO_SANDBOX || 'prod'   // NB: case-sensitive, lowercase 'prod'
const HOST    = process.env.AJO_HOST || 'https://journey-private.adobe.io/authoring'
const API_KEY = process.env.AJO_API_KEY || 'voyager_ui'
const CONCURRENCY = 5

function argVal (flag) {
  const i = process.argv.indexOf(flag)
  return i !== -1 ? process.argv[i + 1] : undefined
}
const ONE_JOURNEY = argVal('--journey')
const OUT_DIR = argVal('--out') || './ajo-journeys'

if (!TOKEN || !ORG_ID) {
  console.error('ERROR: set AJO_BEARER_TOKEN and AJO_ORG_ID environment variables. See README.md.')
  process.exit(1)
}

const headers = {
  'Authorization': `Bearer ${TOKEN}`,
  'x-api-key': API_KEY,
  'x-gw-ims-org-id': ORG_ID,
  'x-sandbox-name': SANDBOX,
  'Content-Type': 'application/json'
}

// ---- helpers ------------------------------------------------------------
const b64 = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64')

async function getJSON (url, extraHeaders) {
  const res = await fetch(url, { method: 'GET', headers: { ...headers, ...(extraHeaders || {}) } })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status} for ${url}\n${body.slice(0, 300)}`)
  }
  return res.json()
}

// Run `fn` over `items` with at most `n` requests in flight.
async function pool (items, n, fn) {
  const out = new Array(items.length)
  let i = 0
  const workers = Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const cur = i++; out[cur] = await fn(items[cur], cur) }
  })
  await Promise.all(workers)
  return out
}

const safeName = (s) => String(s || 'journey').replace(/[^\w.-]+/g, '_').slice(0, 120)

// Recursively pull segment references (inSegment / inAudience) out of a definition.
function extractSegmentRefs (node, out) {
  if (node && typeof node === 'object') {
    if (!Array.isArray(node)) {
      if (node.function === 'inSegment' || node.function === 'inAudience') {
        for (const a of (node.args || [])) {
          const v = a && a.value
          if (typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(v)) out.add(v)
        }
      }
      for (const k of Object.keys(node)) extractSegmentRefs(node[k], out)
    } else {
      for (const v of node) extractSegmentRefs(v, out)
    }
  }
}

// ---- 1) enumerate journeys ---------------------------------------------
async function listJourneys () {
  if (ONE_JOURNEY) return [{ uid: ONE_JOURNEY, name: ONE_JOURNEY }]
  const fields = b64([['uid'], ['name']])
  const sorts  = b64([{ direction: 'ascending', fields: ['name'] }])
  let page = 0, all = []
  for (let guard = 0; guard < 500; guard++) {
    const data = await getJSON(`${HOST}/journeys/`, {
      'x-vyg-query-fields': fields,
      'x-vyg-query-page': String(page),
      'x-vyg-query-pagesize': '100',
      'x-vyg-query-sorts': sorts
    })
    const results = Array.isArray(data.results) ? data.results : []
    all = all.concat(results)
    const pg = data.pagination || {}
    const size = pg.pageSize || 100
    process.stdout.write(`\r  listed ${all.length}/${pg.totalCount ?? '?'} journeys`)
    if ((page + 1) * size >= (pg.totalCount || 0) || results.length === 0) break
    page++
  }
  process.stdout.write('\n')
  return all
}

// ---- 2) fetch each definition + summarise ------------------------------
async function main () {
  mkdirSync(OUT_DIR, { recursive: true })
  console.log(`AJO Journey Exporter -> ${OUT_DIR}  (sandbox: ${SANDBOX})`)
  const journeys = await listJourneys()

  const index = [['journeyUid', 'displayName', 'listName', 'authoringFormatVersion', 'nodeCount', 'segmentRefCount', 'segmentIds', 'file']]
  let ok = 0, failed = 0

  await pool(journeys, CONCURRENCY, async (j) => {
    try {
      const def = await getJSON(`${HOST}/journeys/${j.uid}/latest`)
      const r = def.result || def
      const steps = Array.isArray(r.steps) ? r.steps : []
      const segs = new Set(); extractSegmentRefs(def, segs)
      const displayName = r.name || j.name

      const file = `${safeName(displayName)}__${j.uid}.json`
      writeFileSync(join(OUT_DIR, file), JSON.stringify(def, null, 2))

      index.push([
        j.uid,
        displayName,
        j.name,
        r.authoringFormatVersion || '',
        steps.length,
        segs.size,
        Array.from(segs).join(' '),
        file
      ])
      ok++
      process.stdout.write(`\r  exported ${ok}/${journeys.length}`)
    } catch (err) {
      failed++
      console.error(`\n  ! ${j.uid} (${j.name}): ${err.message}`)
    }
  })
  process.stdout.write('\n')

  // write index.csv
  const csv = index.map(row => row.map(v => {
    const s = String(v ?? '')
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }).join(',')).join('\r\n')
  writeFileSync(join(OUT_DIR, 'index.csv'), csv)

  console.log(`\nDone. ${ok} exported, ${failed} failed.`)
  console.log(`  Per-journey JSON: ${OUT_DIR}/*.json`)
  console.log(`  Summary:          ${OUT_DIR}/index.csv`)
}

main().catch((e) => { console.error('\nFATAL:', e.message); process.exit(1) })
