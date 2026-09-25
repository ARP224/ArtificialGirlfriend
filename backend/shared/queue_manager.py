"""
queue_manager.py

This module enforces a single-task FIFO queue for all LLM-related operations
(e.g., user queries or conversation summarization). By processing tasks one
at a time, it ensures that no two LLM calls (such as generating a reply or
performing a summarization) occur in parallel.

Usage Example:
    from queue_manager import LLMTaskQueue

    # Create a global or shared queue object (usually in init_backend())
    llm_queue = LLMTaskQueue()

    # Define any function that calls the LLM
    def generate_reply_task(user_input: str, backend_reference, character_id: str):
        # This function can safely call the LLM, since only one task is active at a time.
        return backend_reference._do_llm_call(user_input, character_id)

    # Enqueue the task
    future = llm_queue.enqueue_llm_task(generate_reply_task, "Hello!", backend, "girlfriend1")

    # Option A: Wait for the result (blocking):
    result = future.result()

    # Option B: Check or poll the future in an async-friendly environment
    # (e.g., a background thread, callback, or event loop).
"""

import queue
import threading
import concurrent.futures
import logging
import time
import inspect
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

# Import constants from centralized location
from backend.shared.constants import MAX_QUEUE_SIZE, DEFAULT_TASK_TIMEOUT
from backend.shared.awake_clock import awake_seconds

# Set up logger
logger = logging.getLogger(__name__)

# Define type variables for better type hints
T = TypeVar('T')

# Task priority levels
class TaskPriority(IntEnum):
    """Priority levels for queue tasks"""
    HIGH = 0
    NORMAL = 1
    LOW = 2

# Custom exceptions
class QueueFullError(RuntimeError):
    """Raised when the queue has reached its size limit"""
    pass

class TaskTimeoutError(TimeoutError):
    """Raised when a task takes too long to execute"""
    pass

class TaskValidationError(ValueError):
    """Raised when a task fails validation checks"""
    pass

class LLMTaskQueue:
    """
    A FIFO queue manager that processes LLM-related tasks sequentially.
    This prevents concurrency conflicts when multiple tasks (user queries,
    summarization, etc.) request the LLM simultaneously.

    Implementation Details:
    -----------------------
    - We use a single worker thread to pop tasks from a Queue in FIFO order.
    - Each task is a callable plus its arguments/kwargs, wrapped so we can
      store the result or exception in a Future.
    - The consumer thread runs each task, then sets the Future's result or
      exception accordingly.
    - The enqueue_llm_task(...) method immediately returns a Future, allowing
      the caller to either block (future.result()) or handle the result
      asynchronously.
    - Only one LLM task runs at a time, fulfilling the requirement that user
      queries and summarization never run concurrently.
    - On shutdown, a sentinel task (None) is pushed to stop the worker thread
      gracefully.
    - Tasks can be prioritized, with higher priority tasks processed before
      lower priority ones.

    Requirements Reflected:
    -----------------------
    1. Single concurrency point for LLM usage.
    2. Tasks are processed in priority order, then FIFO within each priority.
    3. Safe from concurrency collisions.
    4. Return a handle (Future) so the calling code can retrieve the result.
    5. The queue can handle an arbitrary number of tasks, ensuring subsequent
       tasks wait until the current LLM call completes.
    6. Graceful shutdown that ends the worker thread.
    7. Option to cancel pending tasks on shutdown.

    Thread Safety:
    -------------
    All operations are thread-safe. The queue can be safely called from
    multiple threads.

    Exception Handling:
    ------------------
    Exceptions raised within task callables are caught and stored in the
    corresponding Future. The calling code can access these exceptions
    through future.exception() or they will be re-raised if future.result()
    is called.
    """

    # Define a type for our task tuple
    TaskType = Tuple[Optional[Callable[[], Any]], Optional[concurrent.futures.Future], TaskPriority]

    def __init__(self, max_queue_size: int = MAX_QUEUE_SIZE, default_task_timeout: float = DEFAULT_TASK_TIMEOUT) -> None:
        """
        Initialize the queue and start the worker thread.
        
        Parameters
        ----------
        max_queue_size : int, optional
            Maximum number of tasks allowed in the queue (default: 1000).
            If exceeded, new tasks will be rejected with QueueFullError.
        default_task_timeout : float, optional
            Default timeout in seconds for task execution (default: 300).
            Tasks exceeding this time will be cancelled if possible.
        
        Thread Safety:
        ------------
        This method is not thread-safe and should only be called once
        during application initialization.
        
        Raises
        ------
        RuntimeError
            If worker thread creation fails.
        ValueError
            If max_queue_size or default_task_timeout are invalid.
        """
        # Validate parameters
        if not isinstance(max_queue_size, int) or max_queue_size <= 0:
            raise ValueError(f"max_queue_size must be a positive integer, got: {max_queue_size}")
        if not isinstance(default_task_timeout, (int, float)) or default_task_timeout <= 0:
            raise ValueError(f"default_task_timeout must be a positive number, got: {default_task_timeout}")
        
        # Use PriorityQueue for task prioritization
        self._task_queue: queue.PriorityQueue[LLMTaskQueue.TaskType] = queue.PriorityQueue(maxsize=max_queue_size)
        
        # Configuration
        self._max_queue_size = max_queue_size
        self._default_task_timeout = default_task_timeout
        
        # Threading control objects
        self._lock = threading.RLock()  # Reentrant lock for thread safety
        self._shutdown_flag = threading.Event()
        self._force_shutdown_flag = threading.Event()
        self._graceful_shutdown_flag = threading.Event()  # For graceful shutdown without cancellation
        self._task_available = threading.Event()  # For efficient waiting
        
        # Health monitoring
        self._last_task_start_time = 0.0
        self._last_activity_time = time.time()
        # スリープ耐性: is_healthy のスタック判定用 awake版（スリープ除外）。
        # 表示/ログ(status, current_task_runtime)はwallのまま、判定だけawakeで行う。
        self._last_task_start_awake = 0.0
        self._last_activity_awake = awake_seconds()
        self._task_count = 0
        self._task_sequence = 0  # Sequence number for task ordering
        
        # Auto-restart configuration
        self._restart_count = 0
        self._max_restarts = 3
        self._monitor_thread = None
        
        # Start the worker thread
        with self._lock:
            try:
                self._start_worker()
                self._is_initialized = True
                
                # Start health monitor thread
                self._monitor_thread = threading.Thread(
                    target=self._monitor_worker_health,
                    name="LLMTaskQueueMonitor",
                    daemon=True
                )
                self._monitor_thread.start()
                
                logger.debug("LLMTaskQueue initialized successfully with health monitoring")
            except Exception as e:
                self._is_initialized = False
                logger.critical(f"Failed to initialize LLMTaskQueue: {e}")
                raise RuntimeError(f"Failed to initialize task queue: {e}") from e
    
    def _start_worker(self) -> None:
        """Start or restart the worker thread"""
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name=f"LLMTaskQueueWorker-{self._restart_count}",
            daemon=True
        )
        self._worker_thread.start()
        logger.info(f"Worker thread started (restart count: {self._restart_count})")
    
    def _monitor_worker_health(self) -> None:
        """Background thread to restart worker if it dies"""
        while not self._shutdown_flag.is_set() and not self._graceful_shutdown_flag.is_set():
            try:
                # Use shorter sleep intervals for more responsive shutdown
                for _ in range(10):  # 10 x 0.5s = 5s total
                    if self._shutdown_flag.is_set() or self._graceful_shutdown_flag.is_set():
                        logger.debug("Monitor thread detected shutdown, exiting")
                        return
                    time.sleep(0.5)
                
                with self._lock:
                    # Check if we're shutting down
                    if self._shutdown_flag.is_set() or self._graceful_shutdown_flag.is_set():
                        return
                    
                    # Check if worker is alive
                    if not self._worker_thread.is_alive():
                        if self._restart_count < self._max_restarts:
                            logger.error(f"Worker thread died unexpectedly, attempting restart ({self._restart_count + 1}/{self._max_restarts})")
                            self._restart_count += 1
                            self._start_worker()
                            
                            # Give the new worker some time to start
                            time.sleep(1)
                        else:
                            logger.critical(f"Worker thread died and max restarts ({self._max_restarts}) exceeded. Queue is now non-functional.")
                            # Could trigger an alert or callback here
                            return
            except Exception as e:
                logger.error(f"Error in health monitor: {e}")
                # Continue monitoring unless shutdown
                if self._shutdown_flag.is_set() or self._graceful_shutdown_flag.is_set():
                    return
        
        logger.debug("Monitor thread exiting due to shutdown")

    def _worker_loop(self) -> None:
        """
        Continuously process tasks from the queue, running exactly one at a time.
        If a sentinel (None) task is encountered or the shutdown flag is set,
        the loop ends.
        
        This method runs in a separate worker thread and should not be called directly.
        
        Error handling includes:
        - Task timeout detection and cancellation
        - Deadlock detection with heartbeat
        - Exception isolation (errors in one task don't crash the worker)
        - Graceful shutdown on external signals
        """
        # Initialize heartbeat time
        last_heartbeat = time.time()
        
        # Main worker loop
        while not self._shutdown_flag.is_set() and not self._force_shutdown_flag.is_set():
            # For graceful shutdown, check if we should finish remaining tasks
            if self._graceful_shutdown_flag.is_set() and self._task_queue.empty():
                logger.debug("Graceful shutdown: all tasks completed, exiting worker loop")
                break
            # Update heartbeat
            current_time = time.time()
            if current_time - last_heartbeat > 30:  # Log heartbeat every 30 seconds
                logger.debug("LLMTaskQueue worker thread heartbeat")
                last_heartbeat = current_time
            
            # Wait efficiently for tasks using an event
            if self._task_queue.empty():
                # Wait for a task or shutdown with shorter timeout for responsiveness
                self._task_available.wait(timeout=0.5)
                self._task_available.clear()
                
                # Check shutdown flags after waiting
                if self._shutdown_flag.is_set() or self._force_shutdown_flag.is_set():
                    logger.debug("Worker loop detected shutdown flag while waiting for task")
                    break
                
                # Check graceful shutdown after waiting
                if self._graceful_shutdown_flag.is_set() and self._task_queue.empty():
                    logger.debug("Worker loop detected graceful shutdown with empty queue")
                    break
                
                # If still empty after wait, continue the loop
                if self._task_queue.empty():
                    # Update activity time to show we're still responsive
                    with self._lock:
                        self._last_activity_time = time.time()
                        self._last_activity_awake = awake_seconds()
                    continue

            # Get the next task
            try:
                # Unpack task components
                priority, task_id, (task_callable, future, task_priority) = self._task_queue.get(block=False)
                
                # If we receive a sentinel, break the loop (shutdown)
                if task_callable is None or future is None:
                    self._task_queue.task_done()
                    logger.debug("Worker thread received sentinel task, exiting loop")
                    break

                # Log task execution (debug level)
                logger.debug(f"Executing LLM task (ID: {task_id}, Priority: {TaskPriority(priority).name})")
                
                # Update activity timestamps
                with self._lock:
                    self._last_activity_time = time.time()
                    self._last_task_start_time = time.time()
                    self._last_activity_awake = awake_seconds()
                    self._last_task_start_awake = awake_seconds()
                    self._task_count += 1

                # Task-specific timeout (enqueue_llm_task の timeout 引数) を優先し、
                # 指定なしなら優先度ベースの既定へフォールバック。
                # (旧実装は _task_timeout を setattr するだけで一度も読んでおらず、
                #  ELYTH の 600s 指定が NORMAL 既定の 300s で切られていた)
                task_timeout = getattr(future, '_task_timeout', None) \
                    or self._get_timeout_for_priority(task_priority)
                
                # Run the task safely with timeout, store results in the Future
                self._execute_task_with_timeout(task_callable, future, task_id, task_timeout)
                
                # Mark the task as done in the queue
                self._task_queue.task_done()
                
            except queue.Empty:
                # This shouldn't happen since we checked, but handle it just in case
                logger.debug("Queue unexpectedly empty after non-empty check")
                continue
            except Exception as e:
                # Catch any other unexpected errors in the worker loop itself
                logger.exception(f"Unexpected error in LLM task queue worker: {e}")
                # Continue the loop instead of crashing the worker thread
                continue

        # Log worker exit
        logger.info("LLMTaskQueue worker thread exiting")

    def _get_timeout_for_priority(self, priority: TaskPriority) -> float:
        """
        Calculate timeout based on task priority.
        Higher priority tasks get more time to complete.
        
        Parameters
        ----------
        priority : TaskPriority
            The priority level of the task
            
        Returns
        -------
        float
            Timeout in seconds for this task
        """
        # Adjust timeout based on priority
        if priority == TaskPriority.HIGH:
            return self._default_task_timeout * 1.5  # 50% more time for high priority
        elif priority == TaskPriority.LOW:
            return self._default_task_timeout * 0.5  # 50% less time for low priority
        else:
            return self._default_task_timeout  # Default for normal priority

    @staticmethod
    def _settle_future(future: concurrent.futures.Future, task_id: Any,
                       result: Any = None, exception: Optional[BaseException] = None) -> None:
        """future に結果/例外を書く。待ち手が既に諦めて cancel 済みなら WARNING 1行。

        _enqueue_llm_task 側は自分の上限で future.cancel() して先に抜けるが、
        ワーカーの実行スレッドはそのまま完走する(Python はスレッドを止められない)。
        完走後にここへ来ると set_result/set_exception が InvalidStateError を投げ、
        ERROR トレースバック2本(実害なし)になっていた(2026-08-16 Windows 実機:
        90 秒待ちの relationship 更新)。結果は捨てるしかないので事実だけ記録する。
        """
        if future.cancelled():
            logger.warning(
                f"LLM task finished after its waiter gave up; result discarded (ID: {task_id})")
            return
        try:
            if exception is not None:
                future.set_exception(exception)
            else:
                future.set_result(result)
        except concurrent.futures.InvalidStateError:
            # cancelled() と set_* の間で cancel された(競合)。同じ扱い
            logger.warning(
                f"LLM task finished after its waiter gave up; result discarded (ID: {task_id})")

    def _execute_task_with_timeout(self, task_callable: Callable[[], Any], 
                                 future: concurrent.futures.Future, 
                                 task_id: int,
                                 timeout: float) -> None:
        """
        Execute a task with timeout protection.
        
        Parameters
        ----------
        task_callable : Callable
            The task function to execute
        future : concurrent.futures.Future
            The future to store the result or exception
        task_id : int
            Unique ID for this task (for logging)
        timeout : float
            Maximum execution time in seconds
        """
        if future.cancelled():
            logger.warning(f"Task was cancelled before execution (ID: {task_id})")
            return
            
        # Create a separate thread for the task with timeout protection
        result_queue: queue.Queue = queue.Queue()

        def execute_task():
            try:
                result = task_callable()
                result_queue.put(("result", result))
            except Exception as e:
                result_queue.put(("exception", e))
        
        # Create and start execution thread
        execution_thread = threading.Thread(
            target=execute_task,
            name=f"Task-{task_id}-Executor"
        )
        execution_thread.daemon = True
        
        try:
            start_time = time.time()
            execution_thread.start()
            
            # Wait for result with timeout, checking shutdown flags periodically
            try:
                # Break timeout into smaller chunks to check shutdown flags
                remaining_timeout = timeout
                check_interval = 0.1  # Check shutdown every 100ms
                result_type = None
                result_value = None
                
                while remaining_timeout > 0:
                    # Only interrupt tasks during force shutdown, not graceful shutdown
                    if self._force_shutdown_flag.is_set():
                        logger.debug(f"Task execution interrupted by force shutdown (ID: {task_id})")
                        self._settle_future(future, task_id,
                                            exception=RuntimeError("Task interrupted by force shutdown"))
                        return
                    
                    # Wait for result with short timeout
                    try:
                        wait_time = min(check_interval, remaining_timeout)
                        result_type, result_value = result_queue.get(timeout=wait_time)
                        break  # Got result, exit loop
                    except queue.Empty:
                        # TTS再生中は時計を止める(稜裁定 2026-08-12: 読み上げ=
                        # 生成成功後なのでタイムアウトさせない。対の停止判定が
                        # 呼出側 _enqueue_llm_task の future 待ちにもある)。
                        # 停止は有界(playback_state の docstring 参照)。
                        from backend.shared.playback_state import is_tts_active
                        if not is_tts_active():
                            remaining_timeout -= wait_time
                        continue
                
                # If we got here without a result, it's a timeout
                if result_type is None:
                    raise queue.Empty()  # Trigger timeout handling below
                
                elapsed = time.time() - start_time
                
                if result_type == "result":
                    logger.debug(f"LLM task completed in {elapsed:.2f}s (ID: {task_id})")
                    self._settle_future(future, task_id, result=result_value)
                else:  # exception
                    logger.exception(f"LLM task failed with exception: {result_value} (ID: {task_id})")
                    self._settle_future(future, task_id, exception=result_value)
                    
            except queue.Empty:
                # Task took too long
                elapsed = time.time() - start_time
                # warning 止まり: 待ち手(_enqueue_llm_task)側が同じ障害を
                # ユーザーへ報告済みのため、キュー側からの遅延トーストを
                # 重ねない(稜裁定 2026-08-02)。記録はファイルログに残る。
                logger.warning(f"LLM task timed out after {elapsed:.2f}s (timeout: {timeout}s, ID: {task_id})")
                task_exception = TaskTimeoutError(f"Task execution exceeded timeout of {timeout:.1f}s")
                self._settle_future(future, task_id, exception=task_exception)

                # Python はスレッドを強制終了できない。実行スレッドを放置したまま
                # worker が次タスクへ進むと「LLM 同時実行は1つ」の不変条件が破れるため、
                # 呼出側には TimeoutError を返した上で worker はスレッド終了を待つ
                # (LLM 呼出は自前の HTTP タイムアウトを持つため有限時間で終わる)。
                # force shutdown 時のみ待たずに抜ける。
                while execution_thread.is_alive():
                    if self._force_shutdown_flag.is_set():
                        logger.warning(f"Abandoning timed-out task thread due to force shutdown (ID: {task_id})")
                        break
                    execution_thread.join(timeout=5.0)
                    if execution_thread.is_alive():
                        logger.warning(
                            f"Timed-out task thread still running; worker waits before next task (ID: {task_id})"
                        )

        except Exception as e:
            logger.exception(f"Error in task execution framework: {e} (ID: {task_id})")
            self._settle_future(future, task_id, exception=e)

    def enqueue_llm_task(
        self, 
        task_callable: Callable[..., T], 
        *args: Any, 
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout: Optional[float] = None,
        **kwargs: Any
    ) -> concurrent.futures.Future:
        """
        Enqueue a new LLM-related task. The task_callable is any function
        that internally performs an LLM call. This method returns a Future
        that will eventually hold the function's return value or an exception.

        Parameters
        ----------
        task_callable : Callable
            A callable that encapsulates the logic for a single LLM operation
            (e.g., generating a reply, summarizing conversation).
        *args : Any
            Positional arguments to pass to the task_callable.
        priority : TaskPriority, optional
            The priority level for this task (default: TaskPriority.NORMAL).
            Higher priority tasks are processed before lower priority ones.
        timeout : float, optional
            Task-specific timeout in seconds. If not provided, uses default based on priority.
        **kwargs : Any
            Keyword arguments to pass to the task_callable.

        Returns
        -------
        future : concurrent.futures.Future
            A Future object that will contain the result of the task_callable
            once executed in the worker thread.

        Raises
        ------
        RuntimeError
            If the queue manager has been shut down.
        QueueFullError
            If the queue is at maximum capacity.
        TaskValidationError
            If the task fails validation checks.
        TypeError
            If task_callable is not callable.
        ValueError
            If priority is not a valid TaskPriority enum value.

        Example:
            def summarize_conversation(character_id):
                # Summarization logic calling the LLM
                return "Summary of conversation"

            future = llm_queue.enqueue_llm_task(summarize_conversation, "girlfriend1")
            summary_result = future.result()  # Blocks until done
            
            # or with priority
            future = llm_queue.enqueue_llm_task(
                generate_reply, "Hello", 
                priority=TaskPriority.HIGH
            )
        """
        # Validate priority parameter
        if not isinstance(priority, TaskPriority):
            raise ValueError(f"Invalid priority type: {type(priority).__name__}. Must be TaskPriority enum")
        
        with self._lock:
            # Check initialization
            if not getattr(self, '_is_initialized', False):
                raise RuntimeError("Cannot enqueue task: LLMTaskQueue is not properly initialized")
                
            # Check if we're shutting down
            if self._shutdown_flag.is_set() or self._graceful_shutdown_flag.is_set():
                raise RuntimeError("Cannot enqueue task: LLMTaskQueue has been shut down")
            
            # Check if queue is full
            if self._task_queue.qsize() >= self._max_queue_size:
                logger.error(f"Queue overflow: {self._task_queue.qsize()} items already queued (max: {self._max_queue_size})")
                raise QueueFullError(f"Queue overflow: Maximum queue size ({self._max_queue_size}) reached")
                
            # Validate the callable
            if not callable(task_callable):
                raise TypeError("task_callable must be callable")
            
            # Create a unique sequence number for this task
            self._task_sequence += 1
            task_id = self._task_sequence
            
            # Validate arguments can be called with the function (basic smoke test)
            try:
                # Simple reflection check for required positional args
                sig = inspect.signature(task_callable)
                min_args = sum(1 for p in sig.parameters.values() 
                            if p.default == inspect.Parameter.empty 
                            and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD))
                
                if len(args) < min_args:
                    error_msg = f"Not enough arguments provided. Function requires {min_args}, got {len(args)}"
                    logger.error(f"Task validation error (ID: {task_id}): {error_msg}")
                    raise TaskValidationError(error_msg)
            except TaskValidationError:
                raise  # Re-raise validation errors
            except Exception as e:
                # Other inspection errors are just warnings
                logger.warning(f"Task parameter validation warning (ID: {task_id}): {e}")
            
            # Wrap the user's function and its arguments into a zero-argument callable
            def wrapped_task() -> Any:
                return task_callable(*args, **kwargs)

            # Create future and enqueue the task
            future: concurrent.futures.Future = concurrent.futures.Future()
            
            # Store timeout if provided as a attribute on the future for later use
            if timeout is not None:
                if not isinstance(timeout, (int, float)) or timeout <= 0:
                    raise ValueError(f"timeout must be a positive number, got: {timeout}")
                setattr(future, '_task_timeout', timeout)
            
            # Store tuple with (priority, sequence_num, data) format for PriorityQueue
            try:
                self._task_queue.put_nowait((
                    priority.value,  # First sort by priority (lower value = higher priority)
                    task_id,  # Then by task_id as a proxy for submission order
                    (wrapped_task, future, priority)  # The actual task data
                ))
                logger.debug(f"Task enqueued successfully (ID: {task_id}, Priority: {priority.name})")
            except queue.Full:
                # This shouldn't happen with the size check above, but handle it just in case
                logger.error(f"Queue unexpectedly full when adding task (ID: {task_id})")
                raise QueueFullError(f"Queue overflow: Maximum queue size ({self._max_queue_size}) reached")
            
            # Signal that a task is available
            self._task_available.set()
            
            return future

    def shutdown(self, cancel_pending: bool = False, timeout: float = 5.0, force: bool = False) -> bool:
        """
        Gracefully shut down the queue manager by signaling the worker thread
        to exit.
        
        Parameters
        ----------
        cancel_pending : bool, optional
            If True, cancels all pending tasks with a CancellationError.
            If False (default), processes all queued tasks before shutdown.
        timeout : float, optional
            Maximum time (in seconds) to wait for the worker thread to terminate.
            Default is 5.0 seconds.
        force : bool, optional
            If True, use more aggressive tactics to terminate the worker thread
            if it doesn't respond to normal shutdown within timeout.
            Default is False.
            
        Returns
        -------
        bool
            True if shutdown completed successfully and worker thread terminated.
            False if the worker thread did not terminate within the timeout.

        Thread Safety:
        -------------
        This method is thread-safe and can be called from any thread.
        Multiple calls to this method will only shut down the queue once.
        """
        with self._lock:
            # Avoid shutting down multiple times
            if self._shutdown_flag.is_set() or self._graceful_shutdown_flag.is_set():
                logger.warning("LLMTaskQueue shutdown called multiple times")
                
                # Still need to ensure monitor thread is stopped
                if self._monitor_thread and self._monitor_thread.is_alive():
                    logger.debug("Waiting for monitor thread to stop")
                    self._monitor_thread.join(timeout=1.0)
                
                return not self._worker_thread.is_alive()
                
            # Check if there's a task currently running
            # (初期値は 0.0。旧 `is not None` は常に真で、タスク未実行時に
            #  time.time()-0.0 の「実行中17億秒」という嘘ログを出していた)
            if self._last_task_start_time > 0:
                current_task_runtime = time.time() - self._last_task_start_time
                if current_task_runtime < 1.0:  # Task just started
                    logger.info("Currently executing task (just started)")
                else:
                    logger.info(f"Currently executing task (running for {current_task_runtime:.1f}s)")
            
            # Set appropriate shutdown flag
            if cancel_pending:
                self._shutdown_flag.set()
                logger.info("LLMTaskQueue immediate shutdown initiated (cancelling pending tasks)")
            else:
                self._graceful_shutdown_flag.set()
                logger.info("LLMTaskQueue graceful shutdown initiated (processing remaining tasks)")
            
            # Track any errors during shutdown
            shutdown_errors = []
            
            if cancel_pending:
                # Get all pending tasks and cancel them
                remaining_tasks: List[Tuple[concurrent.futures.Future, TaskPriority]] = []
                
                # Extract and empty the queue with error handling
                try:
                    pending_count = self._task_queue.qsize()
                    if pending_count > 0:
                        logger.info(f"Cancelling {pending_count} pending tasks in queue")
                        
                    while not self._task_queue.empty():
                        try:
                            priority, task_id, (_, future, task_priority) = self._task_queue.get_nowait()
                            if future is not None:
                                remaining_tasks.append((future, task_priority))
                            self._task_queue.task_done()
                        except queue.Empty:
                            break
                        except Exception as e:
                            shutdown_errors.append(f"Error extracting pending task: {e}")
                            logger.exception("Error during task extraction in shutdown")
                except Exception as e:
                    shutdown_errors.append(f"Error accessing task queue: {e}")
                    logger.exception("Error accessing task queue during shutdown")
                
                # Cancel all pending futures with error handling
                tasks_cancelled = 0
                priority_counts = {TaskPriority.HIGH: 0, TaskPriority.NORMAL: 0, TaskPriority.LOW: 0}
                for future, task_priority in remaining_tasks:
                    try:
                        if not future.done() and not future.cancelled():
                            future.cancel()
                            tasks_cancelled += 1
                            priority_counts[task_priority] += 1
                    except Exception as e:
                        shutdown_errors.append(f"Error cancelling future: {e}")
                        logger.exception("Error cancelling future during shutdown")
                
                if tasks_cancelled > 0:
                    logger.info(f"Cancelled {tasks_cancelled} pending LLM tasks during shutdown:")
                    for priority, count in priority_counts.items():
                        if count > 0:
                            logger.info(f"  - {priority.name}: {count} task(s)")
                else:
                    logger.debug("No pending tasks to cancel")
            
            # Handle sentinel based on cancel_pending flag
            if cancel_pending:
                # Put a sentinel immediately to stop processing
                try:
                    self._task_queue.put((
                        TaskPriority.HIGH.value,  # Highest priority for quick processing
                        -1,  # Special task ID for sentinel
                        (None, None, TaskPriority.HIGH)
                    ))
                    # Signal worker to process sentinel
                    self._task_available.set()
                except Exception as e:
                    shutdown_errors.append(f"Error adding sentinel task: {e}")
                    logger.exception("Error adding sentinel task during shutdown")
            else:
                # For graceful shutdown, signal the worker but let it finish tasks naturally
                # The worker will detect shutdown flag after finishing current and pending tasks
                self._task_available.set()
            
        # ここから先は self._lock の外で行う。
        # worker はタスク開始時に同じ _lock を取得する(activity 更新)ため、
        # ロックを保持したまま join すると、取出し済みタスクがある限り worker が
        # ロック待ちでブロックされ join は必ずタイムアウトしていた
        # (=shutdown が常に5秒ブロックして False を返す)。
        # フラグ設定・キュー排出・sentinel 投入まで(上のロック区間)が済んでいれば、
        # join にロックは不要。

        # Wait for worker thread to finish with timeout
        start_wait = time.time()
        self._worker_thread.join(timeout=timeout)

        # Check if thread terminated successfully
        is_worker_terminated = not self._worker_thread.is_alive()
        if not is_worker_terminated:
            logger.warning(f"LLMTaskQueue worker thread did not terminate within {timeout}s timeout")

            if force:
                # Set the force shutdown flag
                logger.warning("Forcing LLMTaskQueue worker thread termination")
                self._force_shutdown_flag.set()

                # Try one more time with a shorter timeout
                self._task_available.set()  # Ensure thread isn't blocking on wait
                self._worker_thread.join(timeout=1.0)
                is_worker_terminated = not self._worker_thread.is_alive()

                if not is_worker_terminated:
                    logger.error(f"Worker thread could not be terminated after {time.time() - start_wait:.1f}s, even with force flag")
                    shutdown_errors.append("Worker thread termination failed even with force flag")
        else:
            logger.info("LLMTaskQueue worker thread shutdown completed")

        # Also stop the monitor thread
        is_monitor_terminated = True
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.debug("Shutting down monitor thread")
            remaining_time = max(0.1, timeout - (time.time() - start_wait))
            self._monitor_thread.join(timeout=remaining_time)
            is_monitor_terminated = not self._monitor_thread.is_alive()

            if not is_monitor_terminated:
                logger.warning("Monitor thread did not terminate within timeout")
                shutdown_errors.append("Monitor thread termination failed")
            else:
                logger.info("Monitor thread shutdown completed")

        # Log any errors encountered during shutdown
        if shutdown_errors:
            logger.warning(f"Shutdown completed with {len(shutdown_errors)} errors: {'; '.join(shutdown_errors)}")

        # Both threads must be terminated for complete shutdown
        is_terminated = is_worker_terminated and is_monitor_terminated
        if is_terminated:
            logger.info("LLMTaskQueue shutdown completed successfully")

        return is_terminated
            
    @property
    def is_shutdown(self) -> bool:
        """
        Check if the queue manager has been shut down.
        
        Returns
        -------
        bool
            True if the queue manager has been shut down, False otherwise.
        """
        return self._shutdown_flag.is_set() or self._graceful_shutdown_flag.is_set()
    
    @property
    def is_healthy(self) -> bool:
        """
        Check if the queue manager is healthy and responsive.
        
        A queue is considered unhealthy if:
        - It's shut down
        - The worker thread isn't alive
        - The worker thread appears to be stuck (no activity for over 10 minutes)
        
        Returns
        -------
        bool
            True if the queue is healthy, False otherwise
        """
        if self.is_shutdown:
            return False
            
        if not self._worker_thread.is_alive():
            return False
            
        # Check for thread deadlock/stuck state
        with self._lock:
            # スリープ耐性: スタック判定は awake時計（スリープ除外）で行う。
            now_awake = awake_seconds()
            # If a task has been running for over 10 minutes and no activity
            if (self._last_task_start_awake > 0 and
                now_awake - self._last_activity_awake > 600 and  # 10 minutes
                now_awake - self._last_task_start_awake > 600):  # 10 minutes
                return False
                
        return True
        
    def get_queue_status(self) -> Dict[str, Any]:
        """
        Get the current status of the queue.
        
        Returns
        -------
        Dict[str, Any]
            Dictionary containing queue status information:
            - queue_size: Number of tasks in the queue
            - is_shutdown: Whether the queue has been shut down
            - worker_alive: Whether the worker thread is still running
            - is_healthy: Overall health status
            - task_count: Total number of tasks processed
            - last_activity_time: Timestamp of last activity
            - worker_restart_count: Number of times worker has been restarted
            - max_restarts_reached: Whether max restart limit has been hit
        """
        with self._lock:
            try:
                status = {
                    "queue_size": self._task_queue.qsize(),
                    "is_shutdown": self.is_shutdown,
                    "worker_alive": self._worker_thread.is_alive(),
                    "is_healthy": self.is_healthy,
                    "task_count": self._task_count,
                    "last_activity_time": self._last_activity_time,
                    "worker_restart_count": self._restart_count,
                    "max_restarts_reached": self._restart_count >= self._max_restarts
                }
                
                # Add information about currently running task
                if self._last_task_start_time > 0:
                    task_runtime = time.time() - self._last_task_start_time
                    status["current_task_runtime"] = task_runtime
                    
                return status
            except Exception as e:
                logger.exception(f"Error getting queue status: {e}")
                # Return minimal status on error
                return {
                    "is_shutdown": self.is_shutdown,
                    "worker_alive": getattr(self, '_worker_thread', threading.Thread()).is_alive(),
                    "error": str(e)
                }
