// Fails the image build if a copy of a Node package older than its first fixed release
// remains anywhere in the image. Prints every copy it finds, so a build log shows where
// a scanner's finding comes from.
const fs = require("fs");
const path = require("path");

const MINIMUM = {
  undici: [6, 29, 1],
  "brace-expansion": [5, 0, 11],
};
const SKIP = new Set(["/proc", "/sys", "/dev"]);

function parse(version) {
  return version.split(/[.+-]/).slice(0, 3).map(Number);
}

function older(version, minimum) {
  const parts = parse(version);
  // Older major lines have their own fixed releases; only the line we pin is checked.
  if (parts[0] !== minimum[0]) return false;
  for (let i = 1; i < 3; i++) {
    if (parts[i] !== minimum[i]) return parts[i] < minimum[i];
  }
  return false;
}

const failures = [];

function walk(dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (!entry.isDirectory() || entry.isSymbolicLink() || SKIP.has(full)) continue;
    if (entry.name in MINIMUM && path.basename(dir) === "node_modules") {
      const manifest = path.join(full, "package.json");
      if (fs.existsSync(manifest)) {
        const { version } = JSON.parse(fs.readFileSync(manifest, "utf8"));
        console.log(`${entry.name} ${version} ${manifest}`);
        if (older(version, MINIMUM[entry.name])) failures.push(manifest);
      }
    }
    walk(full);
  }
}

walk("/");
if (failures.length > 0) {
  console.error(`vulnerable vendored Node packages remain:\n${failures.join("\n")}`);
  process.exit(1);
}
