// Cloudflare Worker - Triggers GitHub Actions at exact time (no queue delays)

export default {
  async scheduled(event, env, ctx) {
    const now = new Date().toISOString();
    const cron = event.cron || '';

    // Determine which workflow to trigger based on cron expression
    let workflowFile, workflowName;
    if (cron.includes('2 2') || cron.includes('12 2')) {
      workflowFile = 'daily-run.yml';
      workflowName = 'Evening Auto-Trade (Lows)';
    } else if (cron.includes('30 15')) {
      workflowFile = 'morning-run.yml';
      workflowName = 'Morning Auto-Trade (Highs)';
    } else {
      console.error(`[${now}] ❌ Unknown cron expression: ${cron}`);
      return;
    }

    console.log(`[${now}] Triggering ${workflowName} (${workflowFile})...`);

    try {
      const response = await fetch(
        `https://api.github.com/repos/weswickcandleco/kalshi-weather-agent/actions/workflows/${workflowFile}/dispatches`,
        {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
            'User-Agent': 'Cloudflare-Worker-Trigger'
          },
          body: JSON.stringify({
            ref: 'main'
          })
        }
      );

      if (response.ok) {
        console.log(`[${now}] ✅ ${workflowName} triggered successfully`);
      } else {
        const error = await response.text();
        console.error(`[${now}] ❌ Failed to trigger ${workflowName}: ${response.status} - ${error}`);
      }
    } catch (err) {
      console.error(`[${now}] ❌ Error: ${err.message}`);
    }
  }
};
