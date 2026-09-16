"""Build the offline manual and a site source tree from an explicit public allowlist."""
import argparse
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = [
    ('overview', 'README.md', 'index.md'),
    ('install', 'docs/INSTALL.md', 'install.md'),
    ('cli', 'docs/CLI.md', 'cli.md'),
    ('agents', 'docs/AUTOMATION.md', 'agents.md'),
    ('preferences', 'docs/PREFERENCES.md', 'preferences.md'),
    ('performance', 'docs/PERFORMANCE.md', 'performance.md'),
    ('contract', 'spec/SPEC.md', 'contract.md'),
]
IMAGES = ['assets/runtop/demo.gif', 'assets/rt/glance.gif', 'assets/runtop/archives.png',
          'assets/brand/logo-on-dark.svg', 'assets/brand/favicon.svg', 'assets/brand/favicon-32.png',
          'assets/brand/icon-512.png', 'assets/brand/social-preview.png']
# The README opens with a themed logo lockup for GitHub. The site header and the offline
# manual carry the name as a plain heading instead.
LOGO_HEADING = re.compile(r'\A<h1>\s*<picture>.*?</picture>\s*</h1>\s*', re.S)
BADGES = re.compile(r'<!-- badges:start -->.*?<!-- badges:end -->\s*', re.S)
SITE = 'https://bcivitcioglu.github.io/runtop/'
REPO = 'https://github.com/bcivitcioglu/runtop/blob/master/'
STAGE = ROOT / '.local/site-docs'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if the bundled manual is stale')
    parser.add_argument('--site', action='store_true', help='stage allowlisted files for the website')
    args = parser.parse_args()
    mapping = {str((ROOT/source).resolve()): output for _,source,output in PUBLIC}
    topics=[]; pages={}
    for key, source, output in PUBLIC:
        original = BADGES.sub('', LOGO_HEADING.sub('# runtop\n\n', (ROOT/source).read_text()))
        def link(match, offline=False):
            label, href = match.groups()
            if '://' in href or href.startswith('#'):
                return match[0]
            path, _, anchor = href.partition('#')
            resolved = str((ROOT/source).parent.joinpath(path).resolve())
            if resolved in mapping:
                target = mapping[resolved]
                if offline:
                    target = SITE + ('' if target == 'index.md' else target.removesuffix('.md') + '/')
            else:
                target = REPO + str(Path(resolved).relative_to(ROOT))
            return f'[{label}]({target}{"#"+anchor if anchor else ""})'
        no_images = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', original)
        content = re.sub(r'\[([^\]]*)\]\(([^)]+)\)', lambda m:link(m,True), no_images)
        topics.append({'id':key,'title':original.splitlines()[0].lstrip('# '),'content':content.strip()+'\n'})
        pages[output] = re.sub(r'(?<!!)\[([^\]]*)\]\(([^)]+)\)', link, original)
    version=re.search(r'^version = "([^"]+)"', (ROOT/'Cargo.toml').read_text(),re.M)[1]
    manual={'schema':'runtop.docs/v1','version':version,'format':'markdown','topics':topics}
    data=json.dumps(manual,ensure_ascii=False,indent=2)+'\n'
    bundle=ROOT/'spec/manual.json'
    if args.check:
        if not bundle.exists() or bundle.read_text()!=data:
            raise SystemExit('Offline manual is stale. Run python3 scripts/build_docs.py.')
    else:
        bundle.write_text(data)
    if args.site:
        if STAGE.exists(): shutil.rmtree(STAGE)
        STAGE.mkdir(parents=True)
        for name,content in pages.items(): (STAGE/name).write_text(content)
        for source in IMAGES:
            target=STAGE/source;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/source,target)
        (STAGE/'manual.json').write_text(data)
        (STAGE/'llms.txt').write_text('# runtop documentation\n\n'+ '\n\n'.join(t['content'] for t in topics))
    print(f'{len(topics)} public topics; offline manual verified' if args.check else f'{len(topics)} public topics; offline manual built')


if __name__ == '__main__': main()
