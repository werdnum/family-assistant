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

