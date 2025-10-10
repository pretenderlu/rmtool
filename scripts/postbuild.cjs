const fs = require('fs');
const path = require('path');

const source = path.resolve(__dirname, '..', 'build', 'server');
const dest = path.resolve(__dirname, '..', 'dist', 'server');

function copyDir(src, dst) {
  if (!fs.existsSync(src)) {
    return;
  }
  if (!fs.existsSync(dst)) {
    fs.mkdirSync(dst, { recursive: true });
  }
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const dstPath = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, dstPath);
    } else {
      fs.copyFileSync(srcPath, dstPath);
    }
  }
}

copyDir(source, dest);
