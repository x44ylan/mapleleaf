"""Capture, visual/OCR facts, evidence persistence and verified dispatch."""
import time

from PIL import Image

from .knowledge import KnowledgeStore
from .observation import make_observation
from .runtime import write_json, event
from .text_facts import WindowsTextReader, read_facts, ticket_template_facts, mulung_ticket_facts, prepare_dimmed_identity
from .vision import annotate


class QuestIO:
    def __init__(self, session, detector, run_dir, *, reader=None):
        self.session,self.detector,self.run_dir=session,detector,run_dir
        self.reader=reader or WindowsTextReader()
        self.sequence=0
        self.frame=None
        self.observation=None
        self.counter_reader=None

    def observe(self):
        self.sequence+=1
        self.frame=self.session.capture_window()
        detection=self.detector.detect(self.frame.rgb)
        stem=f'{self.sequence:04d}'
        path=self.run_dir/f'frame-{stem}.png'
        Image.fromarray(self.frame.rgb).save(path,compress_level=1)
        observation=make_observation(self.frame,detection,self.detector.profile)
        observation['frame_path']=str(path)
        observation['facts']={}
        if detection.state in {'DUNGEONS','DIMENSION_INFO','DIMENSION_UNAVAILABLE'}:
            if self.counter_reader is None:
                from .remaining import CatalogueCounters
                self.counter_reader=CatalogueCounters()
            counters=(self.counter_reader.read(self.frame.rgb) if detection.state=='DUNGEONS'
                      else {'D09':self.counter_reader.read_dimension_detail(self.frame.rgb)})
            observation['facts']['remaining_entries']=counters
        if detection.state in {'HOME','ELITE_INFO','ELITE_TICKETS','ELITE_RUNNING','ELITE_CLEAR','DAILY_TASKS','DUNGEON_QUEUE','DIMENSION_RUNNING','DIMENSION_CLEAR','MULUNG_TICKETS','MULUNG_RUNNING','MULUNG_CLEAR'}:
            if detection.state=='DIMENSION_CLEAR':
                identity_path=self.run_dir/f'identity-{stem}.png'
                identity_meta=prepare_dimmed_identity(self.frame.rgb,identity_path)
                document=self.reader.read(path,identity_image=identity_path)
                document['identity_preprocessing']=identity_meta
            else:
                document=self.reader.read(path)
            write_json(self.run_dir/f'text-{stem}.json',document)
            observation['facts']=read_facts(document,detection.state)
            if detection.state=='ELITE_TICKETS':
                observation['facts']=ticket_template_facts(observation['facts'],observation['anchors'])
            if detection.state=='MULUNG_TICKETS':
                observation['facts']=mulung_ticket_facts(observation['facts'],observation['anchors'])
        observation['annotated_path']=str(self.run_dir/f'annotated-{stem}.png')
        observation_path=self.run_dir/f'observation-{stem}.json'
        write_json(observation_path,observation)
        annotate(self.frame.rgb,detection).save(observation['annotated_path'],compress_level=1)
        with KnowledgeStore(self.run_dir.parent.parent/'data/progress.sqlite') as knowledge:
            knowledge.record_observation(observation_path,observation)
        event(self.run_dir,'observation',sequence=self.sequence,state=detection.state,facts=observation['facts'],frame=str(path))
        self.observation=observation
        return observation

    def send(self,action,observation):
        if observation['frame_path'] != self.observation['frame_path']:
            raise RuntimeError('Dispatch no longer refers to the most recent frame.')
        self.session.dispatch(action,self.frame)
        event(self.run_dir,'input_sent',action=action,frame=observation['frame_path'])

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)
