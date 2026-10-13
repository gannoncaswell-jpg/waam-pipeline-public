import json
import subprocess
import unittest

from waam_pipeline.slicer import wall
from waam_pipeline.studio import build_state, render_studio
from tests.test_studio_controls import NODE, ROOT, _DOM, _ShellNodes


@unittest.skipUnless(NODE, "Node is needed to exercise Help")
class TestStudioHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = build_state(wall(40, layers=2, layer_height_mm=3),
                               workbench=True, workspace="help-test",
                               request_params={"cell_profile": "lc50s"})
        parser = _ShellNodes()
        parser.feed(render_studio(cls.state).split("<script data-app>", 1)[0])
        cls.shell = parser.nodes

    def run_js(self, scenario):
        source = (
            "const SHELL=" + json.dumps(self.shell) + ";\n"
            "const INITIAL=" + json.dumps(self.state) + ";\n" + _DOM + "\n"
            + "const helpModule=require("
            + json.dumps(str(ROOT / "src/waam_pipeline/studio_help.js")) + ");\n"
            + """
for(const node of elements){
  node.isConnected=true;
  if(node.attrs['data-help-topic'])node.dataset.helpTopic=node.attrs['data-help-topic'];
}
Element.prototype.showModal=function(){this.open=true;};
Element.prototype.close=function(){this.open=false;this.dispatch('close');};
Element.prototype.scrollIntoView=function(){this.scrolled=true;};
let busy=false,guest=false;
const navigation=[];
const help=helpModule.create({document,state:()=>S,busy:()=>busy,guest:()=>guest,
  showStep:step=>navigation.push(step),pause:()=>setPlaying(false)});
"""
            + scenario
        )
        result = subprocess.run([NODE], input=source, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reading_the_tutorial_never_changes_a_build_or_calls_the_server(self):
        self.run_js("""
el('wbLh').value='4.25';el('wbArc').value='27';el('wbJobName').value='Keep this build';
const before=JSON.stringify(S),values=elements.filter(n=>n.id.startsWith('wb')).map(n=>n.value);
el('helpBtn').focus();help.open();
assert.equal(el('helpDlg').open,true);assert.equal(playing,false);
assert.equal(el('helpBack').disabled,true);
for(let i=0;i<5;i++)help.next();
assert.equal(el('helpProgress').textContent,'Step 6 of 6');
assert.equal(el('helpNext').textContent,'Done');
help.back();help.next();help.next();
assert.equal(el('helpDlg').open,false);
assert.equal(document.activeElement,el('helpBtn'));
assert.equal(JSON.stringify(S),before);
assert.deepEqual(elements.filter(n=>n.id.startsWith('wb')).map(n=>n.value),values);
assert.equal(requests.length,0);assert.deepEqual(navigation,[]);
""")

    def test_cell_and_guest_guidance_refresh_without_resetting_the_step(self):
        self.run_js("""
help.open();for(let i=0;i<5;i++)help.next();
S.meta.params.cell_profile='hc10';guest=true;help.refresh();
assert.equal(el('helpProgress').textContent,'Step 6 of 6');
assert.match(el('helpSteps').textContent,/Sign in/);
assert.match(el('helpSteps').textContent,/HC10DTP robot exports are unavailable/);
assert.doesNotMatch(el('helpSteps').textContent,/enter a controller job name/);
S.meta.params.cell_profile='lc50s';guest=false;help.refresh();
assert.match(el('helpSteps').textContent,/ASF number from the pendant/);
assert.doesNotMatch(el('helpSteps').textContent,/HC10DTP robot exports/);
assert.equal(requests.length,0);
""")

    def test_load_part_link_focuses_the_native_file_control(self):
        self.run_js("""
help.open();help.next();help.go();
assert.deepEqual(navigation,['prepare']);
assert.equal(document.activeElement,el('stlFile'));
assert.equal(el('stlFile').getAttribute('type'),'file');
assert.equal(requests.length,0);
""")

    def test_open_controls_requires_an_explicit_action_and_respects_busy_state(self):
        self.run_js("""
help.open();help.next();help.next();help.next();
assert.deepEqual(navigation,[]);
busy=true;help.refresh();help.go();
assert.equal(el('helpGo').disabled,true);assert.deepEqual(navigation,[]);
assert.equal(el('helpDlg').open,true);
busy=false;help.go();
assert.deepEqual(navigation,['place']);
assert.equal(el('helpDlg').open,false);
assert.equal(document.activeElement,el('wbOx'));
assert.equal(requests.length,0);
""")

    def test_help_topics_support_keyboard_navigation_and_reopening(self):
        self.run_js("""
help.open();help.next();
let prevented=false;
el('help-tab-start').onkeydown({key:'ArrowRight',preventDefault(){prevented=true;}});
assert.equal(prevented,true);
assert.equal(el('help-controls').hidden,false);
assert.equal(el('help-start').hidden,true);
assert.equal(el('helpWalkActions').hidden,true);
assert.equal(document.activeElement,el('help-tab-controls'));
assert.equal(el('help-tab-controls').getAttribute('aria-selected'),'true');
help.close();help.open();
assert.equal(el('help-controls').hidden,false);
help.chooseTopic('start');
assert.equal(el('helpProgress').textContent,'Step 2 of 6');
assert.equal(el('helpWalkActions').hidden,false);
""")

    def test_saved_preview_only_links_to_available_review_controls(self):
        self.run_js("""
S.meta.workbench=false;help.open();help.go();
assert.equal(el('helpGo').disabled,true);assert.deepEqual(navigation,[]);
for(let i=0;i<4;i++)help.next();
assert.equal(el('helpGo').disabled,false);help.go();
assert.deepEqual(navigation,['review']);
assert.equal(document.activeElement,el('reviewVerdict'));
assert.equal(requests.length,0);
""")
