#!/usr/bin/env node
/**
 * Node.js script to start telegram-test-api server.
 *
 * This script is invoked by the Python TelegramTestServer class
 * to start the test server as a subprocess.
 *
 * Usage: node telegram_test_server_runner.js <port> <host>
 */

const TelegramServer = require('telegram-test-api');

const port = parseInt(process.argv[2]) || 9000;
const host = process.argv[3] || 'localhost';

const config = {
  port: port,
  host: host,
  storage: 'RAM',
  storeTimeout: 60
};

const server = new TelegramServer(config);

// Fix timestamps in responses by patching the Express response prototype.
// telegram-test-api returns JavaScript timestamps (milliseconds) but
// python-telegram-bot expects Unix timestamps (seconds).
function fixTimestamps(obj) {
  if (!obj || typeof obj !== 'object') return obj;

  for (const key in obj) {
    if (key === 'date' && typeof obj[key] === 'number') {
      // If timestamp looks like milliseconds (> year 2100 in seconds), convert to seconds
      if (obj[key] > 4102444800) {
        obj[key] = Math.floor(obj[key] / 1000);
      }
    } else if (typeof obj[key] === 'object') {
      fixTimestamps(obj[key]);
    }
  }
  return obj;
}

// Patch res.json on the Express response prototype to intercept ALL responses
const originalResJson = server.webServer.response.json;
server.webServer.response.json = function(data) {
  return originalResJson.call(this, fixTimestamps(data));
};

server.start().then(() => {
  console.log(`telegram-test-api server started on ${host}:${port}`);
  console.log(`API URL: ${server.config.apiURL}`);
}).catch(err => {
  console.error('Failed to start telegram-test-api:', err);
  process.exit(1);
});

// Handle graceful shutdown
process.on('SIGTERM', async () => {
  console.log('Received SIGTERM, stopping server...');
  await server.stop();
  process.exit(0);
});

process.on('SIGINT', async () => {
  console.log('Received SIGINT, stopping server...');
  await server.stop();
  process.exit(0);
});

