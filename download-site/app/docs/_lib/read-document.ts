import { readFileSync } from "node:fs";
import path from "node:path";

export function readRepositoryDocument(relativePath: string) {
  const repositoryRoot = path.resolve(process.cwd(), "..");
  return readFileSync(path.join(repositoryRoot, relativePath), "utf8");
}
