use pyo3::prelude::*;
use pyo3::types::PyTuple;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// A handle for a scheduled task.
#[pyclass(module = "uringcore")]
pub struct UringHandle {
    callback: PyObject,
    args: Py<PyTuple>,
    #[allow(dead_code)]
    loop_: PyObject,
    context: Option<PyObject>,
    cancelled: Arc<AtomicBool>,
}

#[pymethods]
impl UringHandle {
    #[new]
    #[pyo3(signature = (callback, args, loop_, context=None))]
    fn new(
        callback: PyObject,
        args: Py<PyTuple>,
        loop_: PyObject,
        context: Option<PyObject>,
    ) -> Self {
        Self {
            callback,
            args,
            loop_,
            context,
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Cancel the callback.
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
    }

    /// Return True if the callback was cancelled.
    fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Relaxed)
    }

    /// Execute the callback (Python compatibility wrapper).
    fn _run(&self, py: Python<'_>) -> PyResult<()> {
        if self.cancelled() {
            return Ok(());
        }

        // If we have context, run inside it
        if let Some(ctx) = &self.context {
            // context.run(callback, *args)
            // args is a tuple, we need to unpack it for run?
            // context.run signature: run(callable, *args, **kwargs)
            // So we pass (callback, arg1, arg2...)

            // Constructing the full args list for context.run is tricky efficiently.
            // context.run(callback, *args)
            // We can use call_method1("run", (callback, ...args...))

            // For max speed, we should avoid multiple tuple creations.
            // But context.run requires it.

            // Simpler path: use context.run(func, *args) via python call
            // But we want to do it from Rust.

            // Let's defer strict contextvars optimization and just call `ctx.call_method1("run", (cb, *args))`
            let args_ref = self.args.bind(py);
            // We need to prepend callback to args
            // Takes some tuple manipulation.

            // To be 100% correct with asyncio, we delegate to context.run.
            // Efficient way:
            // let run_args = (self.callback.clone(), ) + self.args;
            // ctx.call_method1("run", run_args)

            // Using PyTuple::new logic
            let mut run_args_vec: Vec<PyObject> = Vec::with_capacity(1 + args_ref.len());
            run_args_vec.push(self.callback.clone_ref(py));
            for item in args_ref.iter() {
                run_args_vec.push(item.unbind());
            }
            let run_args = PyTuple::new(py, run_args_vec)?;

            ctx.call_method1(py, "run", run_args)?;
        } else {
            // No context, direct call
            self.callback.call1(py, self.args.bind(py))?;
        }

        Ok(())
    }

    fn __repr__(&self) -> String {
        format!("<UringHandle cancelled={}>", self.cancelled())
    }
}

impl UringHandle {
    pub(crate) fn new_native(
        callback: PyObject,
        args: Py<PyTuple>,
        loop_: PyObject,
        context: Option<PyObject>,
    ) -> Self {
        Self {
            callback,
            args,
            loop_,
            context,
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Fast path execution called by Scheduler
    pub fn execute(&self, py: Python<'_>) -> PyResult<()> {
        if self.cancelled.load(Ordering::Relaxed) {
            return Ok(());
        }

        // Identical to _run but internal, can be inlined or optimized further
        if let Some(ctx) = &self.context {
            let args_ref = self.args.bind(py);
            let mut run_args_vec: Vec<PyObject> = Vec::with_capacity(1 + args_ref.len());
            run_args_vec.push(self.callback.clone_ref(py));
            for item in args_ref.iter() {
                run_args_vec.push(item.unbind());
            }
            let run_args = PyTuple::new(py, run_args_vec)?;
            ctx.call_method1(py, "run", run_args)?;
        } else {
            self.callback.call1(py, self.args.bind(py))?;
        }
        Ok(())
    }
}
