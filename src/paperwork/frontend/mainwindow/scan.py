import logging
import multiprocessing
import re
import threading
import time

from gi.repository import GLib
from gi.repository import GObject
import pyocr
import pyocr.builders

from paperwork.backend.util import check_spelling
from paperwork.frontend.util.jobs import Job
from paperwork.frontend.util.jobs import JobFactory
from paperwork.frontend.util.canvas.animations import Animation
from paperwork.frontend.util.canvas.animations import ScanAnimation
from paperwork.frontend.util.canvas.animations import SpinnerAnimation
from paperwork.frontend.util.canvas.animators import LinearSimpleAnimator
from paperwork.frontend.util.canvas.animators import LinearCoordAnimator
from paperwork.frontend.util.canvas.drawers import fit
from paperwork.frontend.util.canvas.drawers import LineDrawer
from paperwork.frontend.util.canvas.drawers import PillowImageDrawer
from paperwork.frontend.util.canvas.drawers import RectangleDrawer
from paperwork.frontend.util.canvas.drawers import TargetAreaDrawer


logger = logging.getLogger(__name__)


class JobScan(Job):
    __gsignals__ = {
        'scan-started': (GObject.SignalFlags.RUN_LAST, None, ()),
        'scan-info': (GObject.SignalFlags.RUN_LAST, None,
                      (
                          # expected width
                          GObject.TYPE_INT,
                          # expected height
                          GObject.TYPE_INT,
                      )),
        'scan-chunk': (GObject.SignalFlags.RUN_LAST, None,
                       # line where to put the image
                       (GObject.TYPE_INT,
                        GObject.TYPE_PYOBJECT,)),  # The PIL image
        'scan-done': (GObject.SignalFlags.RUN_LAST, None,
                      (GObject.TYPE_PYOBJECT,  # Pillow image
                      )),
        'scan-canceled': (GObject.SignalFlags.RUN_LAST, None,
                          ()),
    }

    can_stop = True
    priority = 10

    def __init__(self, factory, id, scan_session):
        Job.__init__(self, factory, id)
        self.can_run = False
        self.scan_session = scan_session

    def do(self):
        self.can_run = True
        logger.info("Scan started")
        self.emit('scan-started')

        size = self.scan_session.scan.expected_size
        self.emit('scan-info', size[0], size[1])

        last_line = 0
        try:
            while self.can_run:
                self.scan_session.scan.read()

                next_line = self.scan_session.scan.available_lines[1]
                if (next_line > last_line):
                    chunk = self.scan_session.scan.get_image(last_line, next_line)
                    self.emit('scan-chunk', last_line, chunk)
                    last_line = next_line

                time.sleep(0)  # Give some CPU time to Gtk
            if not self.can_run:
                logger.info("Scan canceled")
                self.emit('scan-canceled')
                return
        except EOFError:
            pass

        img = self.scan_session.images[-1]
        self.emit('scan-done', img)
        logger.info("Scan done")

    def stop(self, will_resume=False):
        self.can_run = False
        self._stop_wait()
        if not will_resume:
            self.scan_session.scan.cancel()


GObject.type_register(JobScan)


class JobFactoryScan(JobFactory):
    def __init__(self, scan_workflow):
        JobFactory.__init__(self, "Scan")
        self.scan_workflow = scan_workflow

    def make(self, scan_session):
        job = JobScan(self, next(self.id_generator), scan_session)
        job.connect("scan-started",
                    lambda job: GLib.idle_add(self.scan_workflow.on_scan_start))
        job.connect("scan-info",
                    lambda job, x, y:
                    GLib.idle_add(self.scan_workflow.on_scan_info, x, y))
        job.connect("scan-chunk",
                    lambda job, line, img_chunk:
                    GLib.idle_add(self.scan_workflow.on_scan_chunk, line,
                                  img_chunk))
        job.connect("scan-done",
                    lambda job, img: GLib.idle_add(self.scan_workflow.on_scan_done,
                                                   img))
        job.connect("scan-canceled", lambda job:
                    GLib.idle_add(self.scan_workflow.on_scan_canceled))
        return job


class _ImgOCRThread(threading.Thread):
    # we don't use jobs here, because we would need 1 scheduler for each job
    # --> too painful and useless

    def __init__(self, name, ocr_tool, langs, angle, img):
        threading.Thread.__init__(self, name="OCR")
        self.name = name
        self.ocr_tool = ocr_tool
        self.langs = langs
        self.angle = angle
        self.img = img
        self.score = -1
        self.boxes = None

    def __compute_ocr_score_with_spell_checking(self, txt):
        return check_spelling(self.langs['spelling'], txt)

    @staticmethod
    def __boxes_to_txt(boxes):
        txt = u""
        for line in boxes:
            txt += line.content + u"\n"
        return txt

    @staticmethod
    def __compute_ocr_score_without_spell_checking(txt):
        """
        Try to evaluate how well the OCR worked.
        Current implementation:
            The score is the number of words only made of 4 or more letters
            ([a-zA-Z])
        """
        # TODO(Jflesch): i18n / l10n
        score = 0
        prog = re.compile(r'^[a-zA-Z]{4,}$')
        for word in txt.split(" "):
            if prog.match(word):
                score += 1
        return (txt, score)

    def run(self):
        SCORE_METHODS = [
            ("spell_checker", self.__compute_ocr_score_with_spell_checking),
            ("lucky_guess", self.__compute_ocr_score_without_spell_checking),
            ("no_score", lambda txt: (txt, 0))
        ]

        logger.info("Running OCR on page orientation %s" % self.name)
        self.boxes = self.ocr_tool.image_to_string(
            self.img, lang=self.langs['ocr'],
            builder=pyocr.builders.LineBoxBuilder())

        txt = self.__boxes_to_txt(self.boxes)

        for score_method in SCORE_METHODS:
            try:
                logger.info("Evaluating score of page orientation (%s)"
                             " using method '%s' ..."
                             % (self.name, score_method[0]))
                (_, self.score) = score_method[1](txt)
                # TODO(Jflesch): For now, we throw away the fixed version of the
                # text:
                # The original version may contain proper nouns, and spell
                # checking could make them disappear
                # However, it would be best if we could keep both versions
                # without increasing too much indexation time
                return
            except Exception, exc:
                logger.error("Scoring method '%s' on orientation %s failed !"
                             % (score_method[0], self.name))
                logger.error("Reason: %s" % exc)


class JobOCR(Job):
    __gsignals__ = {
        'ocr-started': (GObject.SignalFlags.RUN_LAST, None,
                        (GObject.TYPE_PYOBJECT,  # image to ocr
                        )),
        'ocr-angles': (GObject.SignalFlags.RUN_LAST, None,
                       # list of images to ocr: { angle: img }
                       (GObject.TYPE_PYOBJECT,
                       )),
        'ocr-score': (GObject.SignalFlags.RUN_LAST, None,
                      (GObject.TYPE_INT,  # angle
                       GObject.TYPE_FLOAT,  # score
                      )),
        'ocr-done': (GObject.SignalFlags.RUN_LAST, None,
                     (GObject.TYPE_INT,   # angle
                      GObject.TYPE_PYOBJECT,  # image to ocr (rotated)
                      GObject.TYPE_PYOBJECT,  # line + word boxes
                     )),
    }

    can_stop = False
    priority = 5

    OCR_THREADS_POLLING_TIME = 0.1

    def __init__(self, factory, id,
                 ocr_tool, langs, angles, img):
        Job.__init__(self, factory, id)
        self.ocr_tool = ocr_tool
        self.langs = langs
        self.imgs = {angle: img.rotate(angle) for angle in angles}

    def do(self):
        self.emit('ocr-started', self.imgs[0])
        self.emit('ocr-angles', dict(self.imgs))

        max_threads = multiprocessing.cpu_count()
        threads = []
        scores = []

        if len(self.imgs) > 1:
            logger.debug("Will use %d process(es) for OCR" % (max_threads))

        # Run the OCR tools in as many threads as there are processors/core
        # on the computer
        nb = 0
        while (len(self.imgs) > 0 or len(threads) > 0):
            # look for finished threads
            for thread in threads:
                if not thread.is_alive():
                    threads.remove(thread)
                    logger.info("OCR done on angle %d: %f"
                                % (thread.angle, thread.score))
                    scores.append((thread.score, thread.angle,
                                   thread.img, thread.boxes))
                    self.emit('ocr-score', thread.angle, thread.score)
            # start new threads if required
            while (len(threads) < max_threads and len(self.imgs) > 0):
                (angle, img) = self.imgs.popitem()
                logger.info("Starting OCR on angle %d" % angle)
                thread = _ImgOCRThread(str(nb), self.ocr_tool,
                                       self.langs, angle, img)
                thread.start()
                threads.append(thread)
                nb += 1
            time.sleep(self.OCR_THREADS_POLLING_TIME)

        # We want the higher score first
        scores.sort(cmp=lambda x, y: cmp(y[0], x[0]))

        logger.info("Best: %f" % (scores[0][0]))

        self.emit('ocr-done', scores[0][1], scores[0][2], scores[0][3])


GObject.type_register(JobOCR)


class JobFactoryOCR(JobFactory):
    def __init__(self, scan_workflow, config):
        JobFactory.__init__(self, "OCR")
        self.__config = config
        self.scan_workflow = scan_workflow

    def make(self, img, nb_angles):
        angles = range(0, nb_angles * 90, 90)

        ocr_tools = pyocr.get_available_tools()
        if len(ocr_tools) == 0:
            print("No OCR tool found")
            sys.exit(1)
        ocr_tool = ocr_tools[0]
        print("Will use tool '%s'" % (ocr_tool.get_name()))

        job = JobOCR(self, next(self.id_generator), ocr_tool,
                     self.__config.langs, angles, img)
        job.connect("ocr-started", lambda job, img:
                    GLib.idle_add(self.scan_workflow.on_ocr_started, img))
        job.connect("ocr-angles", lambda job, imgs:
                    GLib.idle_add(self.scan_workflow.on_ocr_angles, imgs))
        job.connect("ocr-score", lambda job, angle, score:
                    GLib.idle_add(self.scan_workflow.on_ocr_score, angle, score))
        job.connect("ocr-done", lambda job, angle, img, boxes:
                    GLib.idle_add(self.scan_workflow.on_ocr_done, angle, img,
                                  boxes))
        return job


class ScanWorkflowDrawer(Animation):
    GLOBAL_MARGIN = 10
    SCAN_TO_OCR_ANIM_TIME = 1000  # ms
    IMG_MARGIN = 20

    layer = Animation.IMG_LAYER

    def __init__(self, scan_workflow):
        Animation.__init__(self)

        self.scan_drawers = []

        self.ocr_drawers = {}  # angle --> [drawers]

        self.animators = []
        self._position = (0, 0)

        self.scan_workflow = scan_workflow

        self.__used_angles = None  # == any

        # we are used as a page drawer, but our page is being built
        # --> no actual page
        self.page = None
        self.rotation_done = False

    def __get_size(self):
        assert(self.canvas)
        return (
            self.canvas.visible_size[0],
            self.canvas.visible_size[1],
        )

    size = property(__get_size)
    max_size = property(__get_size)

    def __get_position(self):
        return self._position

    def __set_position(self, position):
        self._position = position
        for drawer in self.scan_drawers:
            drawer.position = (
                position[0] + (self.canvas.visible_size[0] / 2)
                - (drawer.size[0] / 2),
                position[1],
            )

    position = property(__get_position, __set_position)

    def set_size_ratio(self, ratio):
        # we are used as a page drawer, but we don't care about the scale/ratio
        return

    def do_draw(self, cairo_ctx, offset, size):
        for drawer in self.scan_drawers:
            drawer.draw(cairo_ctx, offset, size)
        for drawers in self.ocr_drawers.values():
            for drawer in drawers:
                drawer.draw(cairo_ctx, offset, size)

    def on_tick(self):
        for drawer in self.scan_drawers:
            drawer.on_tick()
        for animator in self.animators:
            animator.on_tick()

    def on_scan_started(self):
        pass

    def on_scan_info(self, x, y):
        size = fit((x, y), self.canvas.visible_size)
        position = (
            self.position[0] + (self.canvas.visible_size[0] / 2)
            - (size[0] / 2),
            self.position[1],
        )

        scan_drawer = ScanAnimation(position, (x, y),
                                    self.canvas.visible_size)
        scan_drawer.set_canvas(self.canvas)
        ratio = scan_drawer.ratio

        self.scan_drawers = [scan_drawer]

        calibration = self.scan_workflow.calibration
        if calibration:
            calibration_drawer = TargetAreaDrawer(
                position, size,
                (
                    int(position[0] + (ratio * calibration[0][0])),
                    int(position[1] + (ratio * calibration[0][1])),
                ),
                (
                    int(ratio * (calibration[1][0] - calibration[0][0])),
                    int(ratio * (calibration[1][1] - calibration[0][1])),
                ),
            )
            calibration_drawer.set_canvas(self.canvas)

            self.scan_drawers.append(calibration_drawer)

        self.canvas.redraw()

    def on_scan_chunk(self, line, img_chunk):
        assert(len(self.scan_drawers) > 0)
        self.scan_drawers[0].add_chunk(line, img_chunk)

    def on_scan_done(self, img):
        pass

    def on_scan_error(self, error):
        self.scan_drawers = []

    def on_scan_canceled(self):
        self.scan_drawers = []

    def __compute_reduced_sizes(self, visible_area, img_size):
        visible_area = (
            visible_area[0] / 2,
            visible_area[1] / 2,
        )
        ratio = min(
            1.0,
            float(visible_area[0]) / float(img_size[0]),
            float(visible_area[1]) / float(img_size[1]),
            float(visible_area[0]) / float(img_size[1]),
            float(visible_area[1]) / float(img_size[0]),
        )
        return (
            int(ratio * img_size[0]) - (2 * self.IMG_MARGIN),
            int(ratio * img_size[1]) - (2 * self.IMG_MARGIN),
        )

    def __compute_reduced_positions(self, visible_area, img_size,
                                    target_img_sizes):
        target_positions = {
            # center positions
            0: (visible_area[0] / 4,
                self.position[1] + (visible_area[1] / 4)),
            90: (visible_area[0] * 3 / 4,
                 self.position[1] + (visible_area[1] / 4)),
            180: (visible_area[0] / 4,
                  self.position[1] + (visible_area[1] * 3 / 4)),
            270: (visible_area[0] * 3 / 4,
                  self.position[1] + (visible_area[1] * 3 / 4)),
        }

        for key in target_positions.keys()[:]:
            # image position
            target_positions[key] = (
                target_positions[key][0] - (target_img_sizes[0] / 2),
                target_positions[key][1] - (target_img_sizes[1] / 2),
            )

        return target_positions

    def on_ocr_started(self, img):
        assert(self.canvas)

        if len(self.scan_drawers) > 0:
            if hasattr(self.scan_drawers[-1], 'target_size'):
                size = self.scan_drawers[-1].target_size
                position = self.scan_drawers[-1].target_position
            else:
                size = self.scan_drawers[-1].size
                position = self.scan_drawers[-1].position
            self.scan_drawers = []
        else:
            size = fit(img.size, self.canvas.visible_size)
            position = self.position

        # animations with big images are too slow
        # --> reduce the image size
        img = img.resize(size)

        target_sizes = self.__compute_reduced_sizes(
            self.canvas.visible_size, size)
        target_positions = self.__compute_reduced_positions(
            self.canvas.visible_size, size, target_sizes)

        self.ocr_drawers = {}

        for angle in target_positions.keys():
            self.ocr_drawers[angle] = [PillowImageDrawer(position, img)]

        self.animators = []
        for (angle, drawers) in self.ocr_drawers.iteritems():
            drawer = drawers[0]
            drawer.size = size
            logger.info("Animator: Angle %d: %s %s -> %s %s"
                        % (angle,
                           str(drawer.position), str(drawer.size),
                           str(target_positions[angle]),
                           str(target_sizes)))

            # reduce the rotation to its minimum
            anim_angle = angle % 360
            if (anim_angle > 180):
                anim_angle = -1 * (360 - anim_angle)

            new_animators = [
                LinearCoordAnimator(
                    drawer, target_positions[angle],
                    self.SCAN_TO_OCR_ANIM_TIME,
                    attr_name='position', canvas=self.canvas),
                LinearCoordAnimator(
                    drawer, target_sizes,
                    self.SCAN_TO_OCR_ANIM_TIME,
                    attr_name='size', canvas=self.canvas),
                LinearSimpleAnimator(
                    drawer, anim_angle,
                    self.SCAN_TO_OCR_ANIM_TIME,
                    attr_name='angle', canvas=self.canvas),
            ]
            # all the animators last the same length of time
            # so any of them is good enough for this signal
            new_animators[0].connect(
                'animator-end', lambda animator:
                GLib.idle_add(self.on_ocr_rotation_anim_done))
            self.animators += new_animators

    def _disable_angle(self, angle):
        img_drawer = self.ocr_drawers[angle][0]
        # cross out the image
        line_drawer = LineDrawer(
            (
                img_drawer.position[0],
                img_drawer.position[1] + img_drawer.size[1]
            ),
            (
                img_drawer.position[0] + img_drawer.size[0],
                img_drawer.position[1]
            ),
            width=5.0
        )
        self.ocr_drawers[angle] = [
            img_drawer,
            line_drawer,
        ]

    def on_ocr_angles(self, imgs):
        # disable all the angles not evaluated
        self.__used_angles = imgs.keys()
        if self.rotation_done:
            for angle in self.ocr_drawers.keys()[:]:
                if angle not in self.__used_angles:
                    self._disable_angle(angle)

    def on_ocr_rotation_anim_done(self):
        self.rotation_done = True
        for angle in self.ocr_drawers.keys()[:]:
            if self.__used_angles and angle not in self.__used_angles:
                self._disable_angle(angle)
            else:
                img_drawer = self.ocr_drawers[angle][0]
                spinner_bg = RectangleDrawer(
                    img_drawer.position, img_drawer.size,
                    inside_color=(0.0, 0.0, 0.0, 0.1),
                    angle=angle,
                )
                spinner = SpinnerAnimation(
                    (
                        (img_drawer.position[0] + (img_drawer.size[0] / 2))
                        - (SpinnerAnimation.ICON_SIZE / 2),
                        (img_drawer.position[1] + (img_drawer.size[1] / 2))
                        - (SpinnerAnimation.ICON_SIZE / 2)
                    )
                )
                self.ocr_drawers[angle] = [img_drawer, spinner_bg, spinner]
                self.animators.append(spinner)

    def on_ocr_score(self, angle, score):
        if angle in self.ocr_drawers:
            self.ocr_drawers[angle] = self.ocr_drawers[angle][:1]
        # TODO(Jflesch): show score

    def on_ocr_done(self, angle, img, boxes):
        self.animators = []

        drawers = self.ocr_drawers[angle]
        drawer = drawers[0]

        # we got out winner. Shoot the others
        self.ocr_drawers = {
            angle: [drawer]
        }

        new_size = fit(drawer.img_size, self.canvas.visible_size)
        new_position = (
            (self.position[0] + (self.canvas.visible_size[0] / 2)
             - (new_size[0] / 2)),
            (self.position[1]),
        )

        self.animators += [
            LinearCoordAnimator(
                drawer, new_position,
                self.SCAN_TO_OCR_ANIM_TIME,
                attr_name='position', canvas=self.canvas),
            LinearCoordAnimator(
                drawer, new_size,
                self.SCAN_TO_OCR_ANIM_TIME,
                attr_name='size', canvas=self.canvas),
        ]
        self.animators[-1].connect('animator-end', lambda animator:
                                   GLib.idle_add(self.scan_workflow.on_ocr_anim_done,
                                                 angle, img, boxes))


class ScanWorkflow(GObject.GObject):
    __gsignals__ = {
        'scan-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'scan-done': (GObject.SignalFlags.RUN_LAST, None,
                      (GObject.TYPE_PYOBJECT,  # PIL image
                      )),
        'scan-canceled': (GObject.SignalFlags.RUN_LAST, None,
                          ()),
        'scan-error': (GObject.SignalFlags.RUN_LAST, None,
                       (GObject.TYPE_STRING,  # Error message
                       )),
        'ocr-start': (GObject.SignalFlags.RUN_LAST, None,
                      (GObject.TYPE_PYOBJECT,  # PIL image
                      )),
        'ocr-done': (GObject.SignalFlags.RUN_LAST, None,
                     (GObject.TYPE_PYOBJECT,  # PIL image
                      GObject.TYPE_PYOBJECT,  # line + word boxes
                     )),
        'ocr-canceled': (GObject.SignalFlags.RUN_LAST, None,
                         ()),
    }

    STEP_SCAN = 0
    STEP_OCR = 1

    def __init__(self, config, scan_scheduler, ocr_scheduler):
        GObject.GObject.__init__(self)
        self.__config = config
        self.schedulers = {
            'scan': scan_scheduler,
            'ocr': ocr_scheduler,
        }

        self.current_step = -1
        self.drawer = ScanWorkflowDrawer(self)

        self.factories = {
            'scan': JobFactoryScan(self),
            'ocr': JobFactoryOCR(self, config),
        }
        self.__resolution = -1
        self.calibration = None

    def scan(self, resolution, scan_session):
        """
        Returns immediately
        Listen for the signal scan-done to get the result
        """
        self.__resolution = resolution

        calibration = self.__config['scanner_calibration'].value
        if calibration:
            (calib_resolution, calibration) = calibration

            self.calibration = (
                (calibration[0][0] * resolution / calib_resolution,
                 calibration[0][1] * resolution / calib_resolution),
                (calibration[1][0] * resolution / calib_resolution,
                 calibration[1][1] * resolution / calib_resolution),
            )

        job = self.factories['scan'].make(scan_session)
        self.schedulers['scan'].schedule(job)
        return job

    def on_scan_start(self):
        self.drawer.on_scan_started()
        self.emit('scan-start')

    def on_scan_info(self, img_x, img_y):
        self.drawer.on_scan_info(img_x, img_y)

    def on_scan_chunk(self, line, img_chunk):
        self.drawer.on_scan_chunk(line, img_chunk)

    def on_scan_done(self, img):
        if self.calibration:
            img = img.crop(
                (
                    self.calibration[0][0],
                    self.calibration[0][1],
                    self.calibration[1][0],
                    self.calibration[1][1]
                )
            )

        self.drawer.on_scan_done(img)
        self.emit('scan-done', img)

    def on_scan_canceled(self):
        self.drawer.on_scan_canceled()
        self.emit('scan-done', None)

    def ocr(self, img, angles=None):
        """
        Returns immediately.
        Listen for the signal ocr-done to get the result
        """
        if angles is None:
            angles = self.__config['ocr_nb_angles'].value
        img.load()
        job = self.factories['ocr'].make(img, angles)
        self.schedulers['ocr'].schedule(job)
        return job

    def on_ocr_started(self, img):
        self.drawer.on_ocr_started(img)
        self.emit('ocr-start', img)

    def on_ocr_angles(self, imgs):
        self.drawer.on_ocr_angles(imgs)

    def on_ocr_score(self, angle, score):
        self.drawer.on_ocr_score(angle, score)

    def on_ocr_done(self, angle, img, boxes):
        self.drawer.on_ocr_done(angle, img, boxes)

    def on_ocr_anim_done(self, angle, img, boxes):
        self.emit('ocr-done', img, boxes)

    def scan_and_ocr(self, resolution, scan_session):
        """
        Convenience function.
        Returns immediately.
        """
        class _ScanOcrChainer(object):
            def __init__(self, scan_workflow):
                scan_workflow.connect("scan-done", self.__start_ocr)

            def __start_ocr(self, scan_workflow, img):
                if img is None:
                    return
                scan_workflow.ocr(img)

        _ScanOcrChainer(self)
        self.scan(resolution, scan_session)


GObject.type_register(ScanWorkflow)