# Ctrl+Tea Drive Bot site (GitHub Pages)

The site is a draft for OAuth branding and user disclosures. Review privacy, retention, contact details and the Google Drive account access disclosure before using it publicly.

To publish, open the GitHub repository **Settings > Pages > Build and deployment**, set **Source: GitHub Actions**. Then go to **Actions > Publish CtrlTea info pages > Run workflow**. The repository already includes `.github/workflows/pages.yml`, which uploads `/docs`. Once the deployment run succeeds, open the actual site and test index.html, privacy.html and terms.html. The first GitHub Pages initialization cannot be performed by this installation's GitHub Actions token (error: `Resource not accessible by integration`), so a repository administrator must enable Pages in Settings once. Do not paste unverified or unpublished URLs into Google OAuth.

Warning: Google's OAuth verification may require a custom domain that you can verify in Search Console. Publishing on the repository's github.io hostname does not by itself guarantee that Google's Authorized Domains or domain-ownership checks will accept the website. Do not claim verification was completed just because Pages is live.

The alternative is a NEW Personal-mode Google Cloud project authorizing only the bot operator's account. That removes the 7-day testing token expiration, but switching projects may make old files created under the previous OAuth app invisible to drive.file scope. Plan and test a migration before changing the production credentials.
