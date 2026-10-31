import fs from "node:fs";
import path from "node:path";

const DATA_DIR = path.resolve(process.cwd(), "public/data");

export interface SiteProps {
  id: string;
  rank: number;
  name: string;
  locality: string | null;
  country: string | null;
  type: string;
  region: string;
  region_label: string;
  years: { a: number; b: number; c: number };
  area_km2: number;
  mean_change_ab: number;
  mean_change_bc: number;
  centroid_lon: number;
  centroid_lat: number;
  bbox: [number, number, number, number];
  before_image: string | null;
  after_image: string | null;
  timeline?: { year: number; image: string }[];
}

export interface RegionSummary {
  region: string;
  region_label: string;
  years: { a: number; b: number; c: number };
  center_lon: number;
  center_lat: number;
  width_km: number;
  height_km: number;
  n_cells: number;
  n_sites: number;
  change_threshold: number;
  min_site_area_km2: number;
  bbox: [number, number, number, number];
}

export interface Summary {
  n_regions: number;
  n_cells: number;
  n_sites: number;
  bbox: [number, number, number, number];
  regions: RegionSummary[];
}

function readJSON<T>(file: string): T {
  const p = path.join(DATA_DIR, file);
  return JSON.parse(fs.readFileSync(p, "utf-8")) as T;
}

export function getSites(): SiteProps[] {
  return readJSON<SiteProps[]>("sites.json");
}

export function getSummary(): Summary {
  return readJSON<Summary>("summary.json");
}

export function getSite(id: string): SiteProps | undefined {
  return getSites().find((s) => s.id === id);
}
